"""Dashboard analytics — ClickHouse queries with optional PostgreSQL enrichment."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from mc_contracts.dashboard import (
    DigitalHelpModuleQuestionsResponse,
    DigitalHelpModuleRequestsResponse,
    DigitalHelpModuleUsageItem,
    DigitalHelpModuleUsageResponse,
    TeamMemberQuestionItem,
)
from mc_contracts.localized import LocalizedString
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.clickhouse.question_sql import QUESTION_EXTRACT_SQL, QUESTION_NORMALIZE_KEY_SQL
from platform_service.db.repositories.module_repository import ModuleRepository

_DIGITAL_HELP_EVENT = "digital_help_used"
_MODULE_REQUESTED_EVENT = "module_requested"
DEFAULT_CHATBOT_DEMAND_PERIOD_DAYS = 30


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return None


class DashboardAnalyticsService:
    def __init__(
        self,
        ch_client: ClickHouseClient,
        session: AsyncSession | None = None,
    ) -> None:
        self._ch = ch_client
        self._session = session

    async def get_digital_help_module_usage(
        self,
        *,
        tenant_id: UUID | None,
        from_date: date,
        to_date: date,
        limit: int = 20,
        offset: int = 0,
    ) -> DigitalHelpModuleUsageResponse:
        """Rank modules by combined digital_help_used + module_requested volume.

        Keyed on ``module_id``. Events without a ``module_id`` are ignored.
        We do not roll up by ``module_family_id`` — a family spans multiple
        versions, so collapsing to a single representative module misattributes
        usage.
        """
        module_counts: dict[UUID, tuple[int, int]] = {}
        for row in await self._query_module_counts(
            tenant_id=tenant_id,
            from_date=from_date,
            to_date=to_date,
        ):
            module_id = _to_uuid(row.get("module_id"))
            if module_id is None:
                continue
            digital_help_count = _to_int(row.get("digital_help_count"))
            module_requested_count = _to_int(row.get("module_requested_count"))
            existing = module_counts.get(module_id, (0, 0))
            module_counts[module_id] = (
                existing[0] + digital_help_count,
                existing[1] + module_requested_count,
            )

        sorted_modules = sorted(
            module_counts.items(),
            key=lambda item: item[1][0] + item[1][1],
            reverse=True,
        )
        total_modules = len(sorted_modules)
        paged_modules = sorted_modules[offset : offset + limit]

        title_by_module: dict[UUID, Any] = {}
        if paged_modules and self._session is not None:
            module_ids = [module_id for module_id, _ in paged_modules]
            modules = await ModuleRepository(self._session).list_modules_by_ids(
                module_ids,
                tenant_id=tenant_id,
            )
            title_by_module = {mod.id: mod for mod in modules}

        items: list[DigitalHelpModuleUsageItem] = []
        for module_id, (digital_help_count, module_requested_count) in paged_modules:
            mod = title_by_module.get(module_id)
            items.append(
                DigitalHelpModuleUsageItem(
                    module_id=module_id,
                    module_family_id=mod.module_family_id if mod else None,
                    digital_help_count=digital_help_count,
                    module_requested_count=module_requested_count,
                    title=mod.title_localized if mod else None,
                )
            )

        return DigitalHelpModuleUsageResponse(
            from_date=from_date,
            to_date=to_date,
            total_digital_help=sum(counts[0] for counts in module_counts.values()),
            total_module_requested=sum(counts[1] for counts in module_counts.values()),
            total_modules=total_modules,
            limit=limit,
            offset=offset,
            modules=items,
        )

    async def get_digital_help_module_questions(
        self,
        *,
        module_id: UUID,
        tenant_id: UUID | None,
        from_date: date,
        to_date: date,
        limit: int = 50,
        offset: int = 0,
    ) -> DigitalHelpModuleQuestionsResponse:
        """Paginated deduplicated chatbot questions for one concrete module."""
        total_questions = await self._count_module_questions(
            module_id=module_id,
            from_date=from_date,
            to_date=to_date,
            tenant_id=tenant_id,
        )
        total_pages = (total_questions + limit - 1) // limit if total_questions > 0 else 0
        rows = await self._fetch_module_questions_page(
            module_id=module_id,
            from_date=from_date,
            to_date=to_date,
            tenant_id=tenant_id,
            limit=limit,
            offset=offset,
        )

        questions: list[TeamMemberQuestionItem] = []
        for row in rows:
            question = str(row.get("question") or "").strip()
            if not question:
                continue
            last_asked_at = _to_datetime(row.get("last_asked_at"))
            if last_asked_at is None:
                continue
            questions.append(
                TeamMemberQuestionItem(
                    question=question,
                    occurrence_count=_to_int(row.get("occurrence_count")),
                    last_asked_at=last_asked_at,
                )
            )

        return DigitalHelpModuleQuestionsResponse(
            module_id=module_id,
            title=await self._module_title(module_id, tenant_id),
            from_date=from_date,
            to_date=to_date,
            questions=questions,
            total_questions=total_questions,
            total_pages=total_pages,
            limit=limit,
            offset=offset,
        )

    async def get_digital_help_module_requests(
        self,
        *,
        module_id: UUID,
        tenant_id: UUID | None,
        from_date: date,
        to_date: date,
    ) -> DigitalHelpModuleRequestsResponse:
        """Aggregate ``module_requested`` count for one concrete module_id."""
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT count() AS module_requested_count
        FROM coaching_events
        WHERE event_type = {{event_type:String}}
          AND event_date >= {{from_date:Date}}
          AND event_date <= {{to_date:Date}}
          AND module_id = {{module_id:UUID}}
        {tenant_clause}"""
        parameters: dict[str, Any] = {
            "event_type": _MODULE_REQUESTED_EVENT,
            "from_date": from_date,
            "to_date": to_date,
            "module_id": module_id,
            **tenant_params,
        }
        rows = await self._ch.query_rows(query, parameters=parameters)
        count = _to_int(rows[0].get("module_requested_count")) if rows else 0
        return DigitalHelpModuleRequestsResponse(
            module_id=module_id,
            title=await self._module_title(module_id, tenant_id),
            from_date=from_date,
            to_date=to_date,
            module_requested_count=count,
        )

    def _tenant_clause(self, tenant_id: UUID | None) -> tuple[str, dict[str, Any]]:
        if tenant_id is None:
            return "", {}
        return "  AND tenant_id = {tenant_id:UUID}\n", {"tenant_id": tenant_id}

    async def _module_title(
        self,
        module_id: UUID,
        tenant_id: UUID | None,
    ) -> LocalizedString | None:
        if self._session is None:
            return None
        modules = await ModuleRepository(self._session).list_modules_by_ids(
            [module_id],
            tenant_id=tenant_id,
        )
        if not modules:
            return None
        return modules[0].title_localized

    async def distinct_chw_by_module_id(
        self,
        *,
        tenant_id: UUID | None,
        from_date: date,
        to_date: date,
    ) -> dict[UUID, set[int]]:
        """Map module_id → distinct chw_ids who used digital help.

        Demand is attributed to the concrete ``module_id`` on each event; events
        without a ``module_id`` are ignored. We deliberately do not fold in
        ``module_family_id`` — a family spans multiple versions, so collapsing to
        a single representative module misattributes demand.
        """
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          module_id,
          groupUniqArray(chw_id) AS chw_ids
        FROM coaching_events
        WHERE event_type = {{event_type:String}}
          AND event_date >= {{from_date:Date}}
          AND event_date <= {{to_date:Date}}
          AND module_id IS NOT NULL
          AND chw_id IS NOT NULL
        {tenant_clause}GROUP BY module_id
        """
        parameters: dict[str, Any] = {
            "event_type": _DIGITAL_HELP_EVENT,
            "from_date": from_date,
            "to_date": to_date,
            **tenant_params,
        }
        rows = await self._ch.query_rows(query, parameters=parameters)
        out: dict[UUID, set[int]] = {}
        for row in rows:
            module_id = _to_uuid(row.get("module_id"))
            if module_id is None:
                continue
            chw_ids = {cid for raw in (row.get("chw_ids") or []) if (cid := _to_int(raw, default=-1)) >= 0}
            if chw_ids:
                out[module_id] = chw_ids
        return out

    async def requestors_for_module(
        self,
        *,
        module_id: UUID,
        from_date: date,
        to_date: date,
        tenant_id: UUID | None = None,
    ) -> list[tuple[int, datetime | None]]:
        """Return (chw_id, last_seen) for chatbot demanders of this exact module.

        Matches strictly on ``module_id`` so demand is never over-attributed from
        a sibling module in the same family into the assign modal.
        """
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          chw_id,
          max(timestamp_utc) AS last_seen
        FROM coaching_events
        WHERE event_type = {{event_type:String}}
          AND event_date >= {{from_date:Date}}
          AND event_date <= {{to_date:Date}}
          AND chw_id IS NOT NULL
          AND module_id = {{module_id:UUID}}
        {tenant_clause}GROUP BY chw_id
        ORDER BY last_seen DESC
        """
        parameters: dict[str, Any] = {
            "event_type": _DIGITAL_HELP_EVENT,
            "from_date": from_date,
            "to_date": to_date,
            "module_id": module_id,
            **tenant_params,
        }
        rows = await self._ch.query_rows(query, parameters=parameters)
        results: list[tuple[int, datetime | None]] = []
        for row in rows:
            chw_id = _to_int(row.get("chw_id"), default=-1)
            if chw_id < 0:
                continue
            last_seen = row.get("last_seen")
            results.append((chw_id, last_seen if isinstance(last_seen, datetime) else None))
        return results

    async def _count_module_questions(
        self,
        *,
        module_id: UUID,
        from_date: date,
        to_date: date,
        tenant_id: UUID | None,
    ) -> int:
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT count() AS total_questions
        FROM (
          SELECT question_key
          FROM (
            SELECT {QUESTION_NORMALIZE_KEY_SQL} AS question_key
            FROM coaching_events
            WHERE module_id = {{module_id:UUID}}
              AND event_type = {{event_type:String}}
              AND event_date >= {{from_date:Date}}
              AND event_date <= {{to_date:Date}}
            {tenant_clause})
          WHERE length(question_key) > 0
          GROUP BY question_key
        )
        """
        parameters: dict[str, Any] = {
            "module_id": module_id,
            "event_type": _DIGITAL_HELP_EVENT,
            "from_date": from_date,
            "to_date": to_date,
            **tenant_params,
        }
        rows = await self._ch.query_rows(query, parameters=parameters)
        if not rows:
            return 0
        return _to_int(rows[0].get("total_questions"))

    async def _fetch_module_questions_page(
        self,
        *,
        module_id: UUID,
        from_date: date,
        to_date: date,
        tenant_id: UUID | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          argMax(raw_question, timestamp_utc) AS question,
          count() AS occurrence_count,
          max(timestamp_utc) AS last_asked_at
        FROM (
          SELECT
            {QUESTION_NORMALIZE_KEY_SQL} AS question_key,
            {QUESTION_EXTRACT_SQL} AS raw_question,
            timestamp_utc
          FROM coaching_events
          WHERE module_id = {{module_id:UUID}}
            AND event_type = {{event_type:String}}
            AND event_date >= {{from_date:Date}}
            AND event_date <= {{to_date:Date}}
          {tenant_clause})
        WHERE length(question_key) > 0
        GROUP BY question_key
        ORDER BY last_asked_at DESC
        LIMIT {{limit:UInt32}}
        OFFSET {{offset:UInt32}}
        """
        parameters: dict[str, Any] = {
            "module_id": module_id,
            "event_type": _DIGITAL_HELP_EVENT,
            "from_date": from_date,
            "to_date": to_date,
            "limit": limit,
            "offset": offset,
            **tenant_params,
        }
        return await self._ch.query_rows(query, parameters=parameters)

    async def _query_module_counts(
        self,
        *,
        tenant_id: UUID | None,
        from_date: date,
        to_date: date,
    ) -> list[dict[str, Any]]:
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          module_id,
          countIf(event_type = {{digital_help:String}}) AS digital_help_count,
          countIf(event_type = {{module_requested:String}}) AS module_requested_count
        FROM coaching_events
        WHERE event_type IN ({{digital_help:String}}, {{module_requested:String}})
          AND event_date >= {{from_date:Date}}
          AND event_date <= {{to_date:Date}}
          AND module_id IS NOT NULL
        {tenant_clause}GROUP BY module_id
        ORDER BY (digital_help_count + module_requested_count) DESC
        """
        parameters: dict[str, Any] = {
            "digital_help": _DIGITAL_HELP_EVENT,
            "module_requested": _MODULE_REQUESTED_EVENT,
            "from_date": from_date,
            "to_date": to_date,
            **tenant_params,
        }
        return await self._ch.query_rows(query, parameters=parameters)
