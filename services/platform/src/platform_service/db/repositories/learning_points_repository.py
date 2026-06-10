"""Persistence for CHW learning points — single table, one row per scored event."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_learning_point_event import CHWLearningPointEvent


class LearningPointsRepository:
    """One insert per telemetry `event_id` (PK); CHW total is SUM(points) over chw_id."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_total_points(self, *, chw_id: int) -> int:
        stmt = select(func.coalesce(func.sum(CHWLearningPointEvent.points), 0)).where(
            CHWLearningPointEvent.chw_id == chw_id
        )
        total = (await self._session.execute(stmt)).scalar_one()
        return int(total)

    async def try_claim_and_increment(
        self,
        *,
        event_id: uuid.UUID,
        chw_id: int,
        tenant_id: uuid.UUID | None,
        delta: int,
        now: datetime | None = None,
    ) -> bool:
        """Insert `(event_id, points=delta)` once; duplicate `event_id` is a no-op.

        Returns True when a new row was inserted; False when `delta` <= 0 or
        `event_id` already exists.
        """
        if delta <= 0:
            return False
        when = now if now is not None else datetime.now(UTC)

        stmt = (
            pg_insert(CHWLearningPointEvent)
            .values(
                event_id=event_id,
                chw_id=chw_id,
                points=delta,
                awarded_at=when,
                tenant_id=tenant_id,
            )
            .on_conflict_do_nothing(index_elements=[CHWLearningPointEvent.event_id])
            .returning(CHWLearningPointEvent.event_id)
        )
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none() is not None
