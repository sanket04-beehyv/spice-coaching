"""Persistence for the precomputed admin module-demand summary snapshot."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module_demand_summary import ModuleDemandSummary


class ModuleDemandSummaryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scope_filter(self, tenant_id: uuid.UUID | None):  # type: ignore[no-untyped-def]
        if tenant_id is None:
            return ModuleDemandSummary.tenant_id.is_(None)
        return ModuleDemandSummary.tenant_id == tenant_id

    async def get(self, tenant_id: uuid.UUID | None) -> ModuleDemandSummary | None:
        stmt = select(ModuleDemandSummary).where(self._scope_filter(tenant_id))
        return (await self._session.execute(stmt)).scalars().first()

    async def upsert(
        self,
        *,
        tenant_id: uuid.UUID | None,
        top_k: int,
        payload_json: dict[str, Any],
        generated_at: datetime,
        computed_at: datetime,
    ) -> None:
        """Replace the snapshot for a scope (one row per tenant / global)."""
        await self._session.execute(delete(ModuleDemandSummary).where(self._scope_filter(tenant_id)))
        now = datetime.now(UTC)
        self._session.add(
            ModuleDemandSummary(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                top_k=top_k,
                payload_json=payload_json,
                generated_at=generated_at,
                computed_at=computed_at,
                created_at=now,
                updated_at=now,
            )
        )
        await self._session.flush()
