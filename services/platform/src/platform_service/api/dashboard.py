"""Dashboard API — ClickHouse-backed analytics for supervisors and administrators.

GET /dashboard/supervisor/{chw_id}       → SupervisorDashboardResponse
GET /dashboard/district/{upazila_id}     → 501 (not implemented)
GET /dashboard/llm-quality               → LLMQualityResponse
GET /dashboard/digital-help-modules      → DigitalHelpModuleUsageResponse
GET /dashboard/digital-help-modules/{module_id}/questions → DigitalHelpModuleQuestionsResponse
GET /dashboard/digital-help-modules/{module_id}/requests  → DigitalHelpModuleRequestsResponse
GET /dashboard/module-creation-suggestions → ModuleCreationSuggestionListResponse
GET /dashboard/module-creation-suggestions/{suggestion_id} → ModuleCreationSuggestionDetailResponse
GET /dashboard/team-activity             → TeamActivityResponse
GET /dashboard/team-activity/users/{user_id}/questions → TeamMemberQuestionsResponse
GET /dashboard/document-usage            → DocumentUsageResponse

Supervisor and LLM quality routes query ClickHouse materialized views.
Team activity is a device-plane organizer report. Document usage reads
``document_view_daily`` (KPIs / documents) and raw ``coaching_events``
(drill-down) in a single response. District dashboard still returns 501 until
implemented.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from mc_contracts.dashboard import (
    CHWSkillSnapshot,
    DigitalHelpModuleQuestionsResponse,
    DigitalHelpModuleRequestsResponse,
    DigitalHelpModuleUsageResponse,
    DocumentUsageResponse,
    LLMQualityResponse,
    ModuleCreationSuggestionDetailResponse,
    ModuleCreationSuggestionListResponse,
    SupervisorDashboardResponse,
    TeamActivityResponse,
    TeamMemberQuestionsResponse,
)
from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import (
    require_chw_id_for_device_route,
    require_organizer_for_device_route,
    resolve_organizer_for_team_activity,
    resolve_tenant_id_for_dashboard,
    resolve_tenant_id_for_device_route,
)
from platform_service.auth.spice_principal import is_admin_principal
from platform_service.auth.spice_user import get_spice_user
from platform_service.config import get_settings
from platform_service.deps import get_clickhouse_client, get_db
from platform_service.services.dashboard_analytics_service import DashboardAnalyticsService
from platform_service.services.document_usage_analytics_service import (
    DocumentUsageAnalyticsService,
    DocumentUsageFilter,
)
from platform_service.services.document_usage_hierarchy import org_user_index
from platform_service.services.module_creation_suggestion_service import (
    ModuleCreationSuggestionService,
)
from platform_service.services.team_activity_service import TeamActivityService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)

_HIERARCHY_SCOPED_ROLES = frozenset({"AM", "PO", "SK"})

FromDate = Annotated[
    date,
    Query(alias="from", description="Inclusive start date (YYYY-MM-DD)."),
]
ToDate = Annotated[
    date,
    Query(alias="to", description="Inclusive end date (YYYY-MM-DD)."),
]


def _not_implemented(endpoint: str) -> AppError:
    logger.info("Dashboard endpoint requested before implementation endpoint=%s", endpoint)
    return AppError(
        ErrorCode.NOT_IMPLEMENTED.value,
        f"{endpoint} analytics are not implemented yet.",
        status=501,
    )


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _require_inclusive_date_range(*, from_date: date, to_date: date) -> None:
    if from_date > to_date:
        raise AppError(
            ErrorCode.VALIDATION_ERROR.value,
            "from_date must be on or before to_date",
            status=422,
        )


def _document_usage_viewer(request: Request) -> tuple[int | None, bool]:
    """Return (viewer_id, unrestricted). Auth-off and non-hierarchy admins are unrestricted."""
    settings = get_settings()
    if not settings.spice_auth_enabled:
        return None, True
    user = get_spice_user(request)
    viewer_id = user.id
    if viewer_id is None:
        return None, True
    org = org_user_index().get(viewer_id)
    if org is not None and org.role in _HIERARCHY_SCOPED_ROLES:
        return viewer_id, False
    return viewer_id, True


def _build_document_usage_filter(
    request: Request,
    *,
    from_date: date,
    to_date: date,
    tenant_id: UUID | None,
    upazila_id: str | None,
    district: str | None,
    po_id: int | None,
    sk_id: int | None,
    user_id: int | None,
    document_id: UUID | None,
) -> DocumentUsageFilter:
    if from_date > to_date:
        raise AppError(
            ErrorCode.VALIDATION_ERROR.value,
            "'from' must be on or before 'to'.",
            status=422,
        )
    resolved_tenant = resolve_tenant_id_for_dashboard(request, tenant_id)
    viewer_id, unrestricted = _document_usage_viewer(request)
    return DocumentUsageFilter(
        from_date=from_date,
        to_date=to_date,
        tenant_id=resolved_tenant,
        upazila=upazila_id.strip() if upazila_id else None,
        district=district.strip() if district else None,
        po_id=po_id,
        sk_id=sk_id,
        user_id=user_id,
        document_id=document_id,
        viewer_id=viewer_id,
        unrestricted_viewer=unrestricted,
    )


@router.get("/supervisor/{chw_id}")
async def chw_dashboard(
    request: Request,
    chw_id: int,
    period_days: int = Query(default=30, ge=1, le=366),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
) -> SupervisorDashboardResponse:
    """CHW skill snapshot and gap summary (ClickHouse Tier 1)."""
    settings = get_settings()
    if settings.spice_auth_enabled:
        user = get_spice_user(request)
        if not is_admin_principal(user):
            chw_id = require_chw_id_for_device_route(request, chw_id)
    tenant_id = resolve_tenant_id_for_dashboard(request, tenant_id)

    # Inner query aggregates each measure once; outer row derives quiz_correct_rate
    # from those sums so sum(quiz_correct) is not repeated in the same SELECT list.
    base_select = """
    SELECT
      cards_shown,
      quiz_attempts,
      quiz_correct,
      (quiz_correct / nullIf(quiz_attempts, 0)) AS quiz_correct_rate,
      digital_help_used,
      incorrect_referrals
    FROM (
      SELECT
        sum(cards_shown) AS cards_shown,
        sum(quiz_attempts) AS quiz_attempts,
        sum(quiz_correct) AS quiz_correct,
        sum(digital_help_used) AS digital_help_used,
        sum(incorrect_referrals) AS incorrect_referrals
      FROM chw_daily_summary
      WHERE chw_id = {chw_id:Int64}
        AND event_date >= (today() - toIntervalDay({period_days:Int32}))
    """
    if tenant_id is None:
        query = base_select + "\n    ) AS chw_agg\n"
        parameters: dict[str, Any] = {
            "chw_id": int(chw_id),
            "period_days": int(period_days),
        }
    else:
        query = base_select + "        AND tenant_id = {tenant_id:UUID}\n    ) AS chw_agg\n"
        parameters = {
            "chw_id": int(chw_id),
            "period_days": int(period_days),
            "tenant_id": tenant_id,
        }

    try:
        rows = await get_clickhouse_client().query_rows(query, parameters=parameters)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("ClickHouse query failed for supervisor dashboard")
        raise AppError(
            ErrorCode.ANALYTICS_UNAVAILABLE.value,
            "Analytics backend unavailable",
            status=502,
        ) from None

    row: dict[str, Any] = rows[0] if rows else {}

    cards_shown = _to_int(row.get("cards_shown"), default=0)
    quiz_attempts = _to_int(row.get("quiz_attempts"), default=0)
    quiz_correct_rate = _to_float(row.get("quiz_correct_rate")) if quiz_attempts else None
    digital_help_used = _to_int(row.get("digital_help_used"), default=0)
    incorrect_referrals = _to_int(row.get("incorrect_referrals"), default=0)

    snapshot = CHWSkillSnapshot(
        chw_id=chw_id,
        digital_help_used=digital_help_used,
        cards_shown=cards_shown,
        cards_accepted=0,
        incorrect_referrals=incorrect_referrals,
        quiz_correct_rate=quiz_correct_rate,
        active_gaps=[],
    )

    return SupervisorDashboardResponse(
        chw_id=chw_id,
        period_days=period_days,
        chw_snapshot=snapshot,
        top_gap_scenarios=[],
        validator_failure_rate=None,
        fallback_rate=None,
    )


@router.get("/district/{upazila_id}")
async def district_dashboard(upazila_id: str, period_days: int = 30) -> None:
    """District-level rollup (ClickHouse Tier 1)."""
    raise _not_implemented("District dashboard")


@router.get("/llm-quality")
async def llm_quality(
    request: Request,
    period_days: int = Query(default=7, ge=1, le=366),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
) -> LLMQualityResponse:
    """LLM quality metrics (validator_status breakdown, fallback rate)."""
    tenant_id = resolve_tenant_id_for_dashboard(request, tenant_id)

    # Inner query sums each column once; outer row derives rates without repeating sums.
    base_select = """
    SELECT
      digital_help_event_count,
      inference_online_count,
      inference_edge_count,
      inference_offline_count,
      validator_pass_count,
      validator_fail_count,
      fallback_used_count,
      (validator_fail_count / nullIf(digital_help_event_count, 0)) AS validator_failure_rate,
      (fallback_used_count / nullIf(digital_help_event_count, 0)) AS fallback_rate
    FROM (
      SELECT
        sum(digital_help_event_count) AS digital_help_event_count,
        sum(inference_online_count) AS inference_online_count,
        sum(inference_edge_count) AS inference_edge_count,
        sum(inference_offline_count) AS inference_offline_count,
        sum(validator_pass_count) AS validator_pass_count,
        sum(validator_fail_count) AS validator_fail_count,
        sum(fallback_used_count) AS fallback_used_count
      FROM llm_daily_summary
      WHERE event_date >= (today() - toIntervalDay({period_days:Int32}))
    """
    if tenant_id is None:
        query = base_select + "\n    ) AS llm_agg\n"
        parameters: dict[str, Any] = {"period_days": int(period_days)}
    else:
        query = base_select + "        AND tenant_id = {tenant_id:UUID}\n    ) AS llm_agg\n"
        parameters = {"period_days": int(period_days), "tenant_id": tenant_id}

    try:
        rows = await get_clickhouse_client().query_rows(
            query,
            parameters=parameters,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("ClickHouse query failed for LLM quality dashboard")
        raise AppError(
            ErrorCode.ANALYTICS_UNAVAILABLE.value,
            "Analytics backend unavailable",
            status=502,
        ) from None

    row: dict[str, Any] = rows[0] if rows else {}

    digital_help_event_count = _to_int(row.get("digital_help_event_count"), default=0)
    total_inferences = digital_help_event_count

    # If there were no calls in the selected window, ClickHouse expressions return NULLs.
    return LLMQualityResponse(
        period_days=period_days,
        total_inferences=total_inferences,
        digital_help_event_count=digital_help_event_count,
        inference_online_count=_to_int(row.get("inference_online_count"), default=0),
        inference_edge_count=_to_int(row.get("inference_edge_count"), default=0),
        inference_offline_count=_to_int(row.get("inference_offline_count"), default=0),
        validator_pass_count=_to_int(row.get("validator_pass_count"), default=0),
        validator_fail_count=_to_int(row.get("validator_fail_count"), default=0),
        fallback_used_count=_to_int(row.get("fallback_used_count"), default=0),
        avg_latency_ms=None,
        validator_failure_rate=_to_float(row.get("validator_failure_rate")) if total_inferences else None,
        fallback_rate=_to_float(row.get("fallback_rate")) if total_inferences else None,
        error_rate=None,
        avg_input_tokens=None,
        avg_output_tokens=None,
    )


@router.get("/digital-help-modules")
async def digital_help_module_usage(
    request: Request,
    from_date: date = Query(..., description="UTC start date (inclusive)."),
    to_date: date = Query(..., description="UTC end date (inclusive)."),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
) -> DigitalHelpModuleUsageResponse:
    """Rank modules by combined digital_help_used + module_requested volume (keyed on module_id)."""
    _require_inclusive_date_range(from_date=from_date, to_date=to_date)
    tenant_id = resolve_tenant_id_for_dashboard(request, tenant_id)
    try:
        return await DashboardAnalyticsService(
            get_clickhouse_client(), session
        ).get_digital_help_module_usage(
            tenant_id=tenant_id,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("ClickHouse query failed for digital help module usage")
        raise AppError(
            ErrorCode.ANALYTICS_UNAVAILABLE.value,
            "Analytics backend unavailable",
            status=502,
        ) from None


@router.get("/digital-help-modules/{module_id}/questions")
async def digital_help_module_questions(
    request: Request,
    module_id: UUID,
    from_date: date = Query(..., description="UTC start date (inclusive)."),
    to_date: date = Query(..., description="UTC end date (inclusive)."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
) -> DigitalHelpModuleQuestionsResponse:
    """Paginated chatbot questions for one module (keyed on module_id)."""
    _require_inclusive_date_range(from_date=from_date, to_date=to_date)
    tenant_id = resolve_tenant_id_for_dashboard(request, tenant_id)
    try:
        return await DashboardAnalyticsService(
            get_clickhouse_client(), session
        ).get_digital_help_module_questions(
            module_id=module_id,
            tenant_id=tenant_id,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("ClickHouse query failed for digital help module questions")
        raise AppError(
            ErrorCode.ANALYTICS_UNAVAILABLE.value,
            "Analytics backend unavailable",
            status=502,
        ) from None


@router.get("/digital-help-modules/{module_id}/requests")
async def digital_help_module_requests(
    request: Request,
    module_id: UUID,
    from_date: date = Query(..., description="UTC start date (inclusive)."),
    to_date: date = Query(..., description="UTC end date (inclusive)."),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
) -> DigitalHelpModuleRequestsResponse:
    """Aggregate module_requested count for one concrete module_id."""
    _require_inclusive_date_range(from_date=from_date, to_date=to_date)
    tenant_id = resolve_tenant_id_for_dashboard(request, tenant_id)
    try:
        return await DashboardAnalyticsService(
            get_clickhouse_client(), session
        ).get_digital_help_module_requests(
            module_id=module_id,
            tenant_id=tenant_id,
            from_date=from_date,
            to_date=to_date,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("ClickHouse query failed for digital help module requests")
        raise AppError(
            ErrorCode.ANALYTICS_UNAVAILABLE.value,
            "Analytics backend unavailable",
            status=502,
        ) from None


@router.get("/module-creation-suggestions")
async def list_module_creation_suggestions(
    request: Request,
    from_date: date = Query(..., description="UTC start date (inclusive)."),
    to_date: date = Query(..., description="UTC end date (inclusive)."),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
) -> ModuleCreationSuggestionListResponse:
    """List daily module-creation suggestions inferred from unattributed demand."""
    _require_inclusive_date_range(from_date=from_date, to_date=to_date)
    tenant_id = resolve_tenant_id_for_dashboard(request, tenant_id)
    return await ModuleCreationSuggestionService(session).list_suggestions(
        tenant_id=tenant_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        offset=offset,
    )


@router.get("/module-creation-suggestions/{suggestion_id}")
async def get_module_creation_suggestion(
    request: Request,
    suggestion_id: UUID,
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
) -> ModuleCreationSuggestionDetailResponse:
    """Detail for one suggestion including chat questions and free-text requests."""
    tenant_id = resolve_tenant_id_for_dashboard(request, tenant_id)
    try:
        return await ModuleCreationSuggestionService(session).get_detail(
            suggestion_id=suggestion_id,
            tenant_id=tenant_id,
        )
    except LookupError as exc:
        raise AppError(ErrorCode.NOT_FOUND.value, str(exc), status=404) from exc


@router.get("/team-activity")
async def team_activity(
    request: Request,
    from_date: date = Query(..., description="UTC start date (inclusive)."),
    to_date: date = Query(..., description="UTC end date (inclusive)."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> TeamActivityResponse:
    """Team activity report for authenticated organizers (device plane)."""
    _require_inclusive_date_range(from_date=from_date, to_date=to_date)

    organizer_id = resolve_organizer_for_team_activity(request)
    tenant_id = resolve_tenant_id_for_device_route(request, None)
    settings = get_settings()
    uuid_to_spice_id = {v: k for k, v in settings.spice_tenant_uuid_by_id.items()}
    organization_ids = (
        [uuid_to_spice_id[tenant_id]] if tenant_id is not None and tenant_id in uuid_to_spice_id else None
    )

    try:
        return await TeamActivityService(get_clickhouse_client(), session).get_team_activity(
            organizer_id=organizer_id,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
            tenant_id=tenant_id,
            organization_ids=organization_ids,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("ClickHouse query failed for team activity dashboard")
        raise AppError(
            ErrorCode.ANALYTICS_UNAVAILABLE.value,
            "Analytics backend unavailable",
            status=502,
        ) from None


@router.get("/team-activity/users/{user_id}/questions")
async def team_member_questions(
    request: Request,
    user_id: int,
    po_user_id: int | None = Query(
        default=None,
        description="PO user ID whose team to fetch. Required when auth is disabled or caller is an admin principal.",
    ),
    from_date: date = Query(..., description="UTC start date (inclusive)."),
    to_date: date = Query(..., description="UTC end date (inclusive)."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> TeamMemberQuestionsResponse:
    """Paginated chatbot questions for one team member (device plane)."""
    _require_inclusive_date_range(from_date=from_date, to_date=to_date)

    organizer_id = require_organizer_for_device_route(request, po_user_id)
    tenant_id = resolve_tenant_id_for_device_route(request, None)

    try:
        return await TeamActivityService(get_clickhouse_client(), session).get_member_questions(
            organizer_id=organizer_id,
            user_id=user_id,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
            tenant_id=tenant_id,
        )
    except AppError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("ClickHouse query failed for team member questions")
        raise AppError(
            ErrorCode.ANALYTICS_UNAVAILABLE.value,
            "Analytics backend unavailable",
            status=502,
        ) from None


@router.get("/document-usage")
async def document_usage(
    request: Request,
    from_date: FromDate,
    to_date: ToDate,
    tenant_id: UUID | None = Query(default=None),
    upazila_id: str | None = Query(
        default=None,
        description="Filter by org-map upazila (same semantics as district).",
    ),
    district: str | None = Query(default=None),
    po_id: int | None = Query(default=None),
    sk_id: int | None = Query(default=None),
    user_id: int | None = Query(default=None),
    document_id: UUID | None = Query(default=None),
    top_limit: int = Query(default=10, ge=1, le=50),
    documents_limit: int = Query(default=20, ge=1, le=100),
    documents_offset: int = Query(default=0, ge=0),
    events_limit: int = Query(default=50, ge=1, le=200),
    events_offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db),
) -> DocumentUsageResponse:
    """Document-view KPIs, per-document table, and event drill-down in one response."""
    filters = _build_document_usage_filter(
        request,
        from_date=from_date,
        to_date=to_date,
        tenant_id=tenant_id,
        upazila_id=upazila_id,
        district=district,
        po_id=po_id,
        sk_id=sk_id,
        user_id=user_id,
        document_id=document_id,
    )
    try:
        return await DocumentUsageAnalyticsService(get_clickhouse_client(), session).get_usage(
            filters,
            top_limit=top_limit,
            documents_limit=documents_limit,
            documents_offset=documents_offset,
            events_limit=events_limit,
            events_offset=events_offset,
        )
    except AppError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("ClickHouse query failed for document usage")
        raise AppError(
            ErrorCode.ANALYTICS_UNAVAILABLE.value,
            "Analytics backend unavailable",
            status=502,
        ) from None
