"""Dashboard analytics — ClickHouse queries with optional PostgreSQL enrichment."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from mc_contracts.dashboard import (
    DigitalHelpModuleUsageItem,
    DigitalHelpModuleUsageResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.db.repositories.module_repository import ModuleRepository

_DIGITAL_HELP_EVENT = "digital_help_used"
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
        period_days: int,
        limit: int = 20,
        offset: int = 0,
    ) -> DigitalHelpModuleUsageResponse:
        """Rank modules by digital_help_used event volume, keyed on module_id.

        Events without a ``module_id`` are ignored. We do not roll up by
        ``module_family_id`` — a family spans multiple versions, so collapsing
        to a single representative module misattributes usage.
        """
        module_counts: dict[UUID, int] = {}
        for row in await self._query_module_counts(tenant_id=tenant_id, period_days=period_days):
            module_id = _to_uuid(row.get("module_id"))
            if module_id is None:
                continue
            module_counts[module_id] = module_counts.get(module_id, 0) + _to_int(row.get("query_count"))

        sorted_modules = sorted(module_counts.items(), key=lambda item: item[1], reverse=True)
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
        for module_id, query_count in paged_modules:
            mod = title_by_module.get(module_id)
            items.append(
                DigitalHelpModuleUsageItem(
                    module_id=module_id,
                    module_family_id=mod.module_family_id if mod else None,
                    query_count=query_count,
                    title=mod.title_localized if mod else None,
                )
            )

        return DigitalHelpModuleUsageResponse(
            period_days=period_days,
            total_queries=sum(module_counts.values()),
            total_modules=total_modules,
            limit=limit,
            offset=offset,
            modules=items,
        )

    def _tenant_clause(self, tenant_id: UUID | None) -> tuple[str, dict[str, Any]]:
        if tenant_id is None:
            return "", {}
        return "  AND tenant_id = {tenant_id:UUID}\n", {"tenant_id": tenant_id}

    async def distinct_chw_by_module_id(
        self,
        *,
        tenant_id: UUID | None,
        period_days: int = DEFAULT_CHATBOT_DEMAND_PERIOD_DAYS,
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
          AND event_date >= (today() - toIntervalDay({{period_days:Int32}}))
          AND module_id IS NOT NULL
          AND chw_id IS NOT NULL
        {tenant_clause}GROUP BY module_id
        """
        parameters: dict[str, Any] = {
            "event_type": _DIGITAL_HELP_EVENT,
            "period_days": int(period_days),
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
        tenant_id: UUID | None = None,
        period_days: int = DEFAULT_CHATBOT_DEMAND_PERIOD_DAYS,
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
          AND event_date >= (today() - toIntervalDay({{period_days:Int32}}))
          AND chw_id IS NOT NULL
          AND module_id = {{module_id:UUID}}
        {tenant_clause}GROUP BY chw_id
        ORDER BY last_seen DESC
        """
        parameters: dict[str, Any] = {
            "event_type": _DIGITAL_HELP_EVENT,
            "period_days": int(period_days),
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

    async def _query_module_counts(
        self,
        *,
        tenant_id: UUID | None,
        period_days: int,
    ) -> list[dict[str, Any]]:
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          module_id,
          count() AS query_count
        FROM coaching_events
        WHERE event_type = '{_DIGITAL_HELP_EVENT}'
          AND event_date >= (today() - toIntervalDay({{period_days:Int32}}))
          AND module_id IS NOT NULL
        {tenant_clause}GROUP BY module_id
        ORDER BY query_count DESC
        """
        parameters = {"period_days": int(period_days), **tenant_params}
        return await self._ch.query_rows(query, parameters=parameters)
