"""Dashboard analytics — ClickHouse queries with optional PostgreSQL enrichment."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mc_contracts.dashboard import DigitalHelpModuleUsageItem, DigitalHelpModuleUsageResponse
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.db.repositories.module_repository import ModuleRepository

_DIGITAL_HELP_EVENT = "digital_help_used"


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
        family_counts: dict[UUID, int] = {}

        for row in await self._query_family_counts(tenant_id=tenant_id, period_days=period_days):
            family_id = _to_uuid(row.get("module_family_id"))
            if family_id is None:
                continue
            family_counts[family_id] = family_counts.get(family_id, 0) + _to_int(row.get("query_count"))

        module_rows = await self._query_module_id_only_counts(
            tenant_id=tenant_id,
            period_days=period_days,
        )
        if module_rows and self._session is not None:
            module_ids = [
                module_id for row in module_rows if (module_id := _to_uuid(row.get("module_id"))) is not None
            ]
            modules = await ModuleRepository(self._session).list_modules_by_ids(
                module_ids,
                tenant_id=tenant_id,
            )
            module_to_family = {mod.id: mod.module_family_id for mod in modules}
            for row in module_rows:
                module_id = _to_uuid(row.get("module_id"))
                if module_id is None:
                    continue
                family_id = module_to_family.get(module_id)
                if family_id is None:
                    continue
                count = _to_int(row.get("query_count"))
                family_counts[family_id] = family_counts.get(family_id, 0) + count

        sorted_families = sorted(family_counts.items(), key=lambda item: item[1], reverse=True)
        total_modules = len(sorted_families)
        paged_families = sorted_families[offset : offset + limit]

        title_by_family: dict[UUID, Any] = {}
        if paged_families and self._session is not None:
            family_ids = [family_id for family_id, _ in paged_families]
            title_by_family = await ModuleRepository(self._session).list_latest_published_by_family_ids(
                family_ids,
                tenant_id=tenant_id,
            )

        items: list[DigitalHelpModuleUsageItem] = []
        for family_id, query_count in paged_families:
            mod = title_by_family.get(family_id)
            items.append(
                DigitalHelpModuleUsageItem(
                    module_family_id=family_id,
                    module_id=mod.id if mod else None,
                    query_count=query_count,
                    title=mod.title_localized if mod else None,
                )
            )

        return DigitalHelpModuleUsageResponse(
            period_days=period_days,
            total_queries=sum(family_counts.values()),
            total_modules=total_modules,
            limit=limit,
            offset=offset,
            modules=items,
        )

    def _tenant_clause(self, tenant_id: UUID | None) -> tuple[str, dict[str, Any]]:
        if tenant_id is None:
            return "", {}
        return "  AND tenant_id = {tenant_id:UUID}\n", {"tenant_id": tenant_id}

    async def _query_family_counts(
        self,
        *,
        tenant_id: UUID | None,
        period_days: int,
    ) -> list[dict[str, Any]]:
        tenant_clause, tenant_params = self._tenant_clause(tenant_id)
        query = f"""
        SELECT
          module_family_id,
          count() AS query_count
        FROM coaching_events
        WHERE event_type = '{_DIGITAL_HELP_EVENT}'
          AND event_date >= (today() - toIntervalDay({{period_days:Int32}}))
          AND module_family_id IS NOT NULL
        {tenant_clause}GROUP BY module_family_id
        ORDER BY query_count DESC
        """
        parameters = {"period_days": int(period_days), **tenant_params}
        return await self._ch.query_rows(query, parameters=parameters)

    async def _query_module_id_only_counts(
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
          AND module_family_id IS NULL
          AND module_id IS NOT NULL
        {tenant_clause}GROUP BY module_id
        """
        parameters = {"period_days": int(period_days), **tenant_params}
        return await self._ch.query_rows(query, parameters=parameters)
