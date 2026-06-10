"""Read queries for behavioural_gap catalog rows."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.tenant_scope import tenant_scope_filter


class BehaviouralGapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active_updated_since(
        self,
        since: datetime | None,
        *,
        tenant_id: UUID | None = None,
    ) -> list[BehaviouralGap]:
        stmt = select(BehaviouralGap).where(BehaviouralGap.status == "active")
        if since is not None:
            stmt = stmt.where(BehaviouralGap.updated_at > since)
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(BehaviouralGap.tenant_id, tenant_id))
        stmt = stmt.order_by(BehaviouralGap.updated_at.asc(), BehaviouralGap.id.asc())
        return list((await self._session.execute(stmt)).scalars().all())
