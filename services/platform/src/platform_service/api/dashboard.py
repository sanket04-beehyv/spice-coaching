"""Dashboard API — ClickHouse-backed analytics for supervisors and administrators.

GET /dashboard/supervisor/{chw_id}       → SupervisorDashboardResponse
GET /dashboard/district/{upazila_id}     → 501 (not implemented)
GET /dashboard/llm-quality               → LLMQualityResponse
GET /dashboard/digital-help-modules      → DigitalHelpModuleUsageResponse

Supervisor and LLM quality routes query ClickHouse materialized views.
District dashboard still returns 501 until implemented.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from mc_contracts.dashboard import (
    CHWSkillSnapshot,
    DigitalHelpModuleUsageResponse,
    LLMQualityResponse,
    SupervisorDashboardResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import (
    require_chw_id_for_device_route,
    resolve_tenant_id_for_dashboard,
)
from platform_service.auth.spice_principal import is_admin_principal
from platform_service.auth.spice_user import get_spice_user
from platform_service.config import get_settings
from platform_service.deps import get_clickhouse_client, get_db
from platform_service.services.dashboard_analytics_service import DashboardAnalyticsService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
logger = logging.getLogger(__name__)


def _not_implemented(endpoint: str) -> HTTPException:
    logger.info("Dashboard endpoint requested before implementation endpoint=%s", endpoint)
    return HTTPException(status_code=501, detail=f"{endpoint} analytics are not implemented yet.")


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
        raise HTTPException(status_code=502, detail="Analytics backend unavailable") from None

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
        raise HTTPException(status_code=502, detail="Analytics backend unavailable") from None

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
    period_days: int = Query(default=30, ge=1, le=366),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
) -> DigitalHelpModuleUsageResponse:
    """Rank modules by digital_help_used query volume for a tenant (keyed on module_id)."""
    tenant_id = resolve_tenant_id_for_dashboard(request, tenant_id)
    try:
        return await DashboardAnalyticsService(
            get_clickhouse_client(), session
        ).get_digital_help_module_usage(
            tenant_id=tenant_id,
            period_days=period_days,
            limit=limit,
            offset=offset,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("ClickHouse query failed for digital help module usage")
        raise HTTPException(status_code=502, detail="Analytics backend unavailable") from None
