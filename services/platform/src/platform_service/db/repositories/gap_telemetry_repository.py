"""Idempotent claim ledger for gap-state telemetry processing."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_gap_telemetry_event import CHWGapTelemetryEvent


class GapTelemetryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_claim_event(
        self,
        *,
        event_id: uuid.UUID,
        chw_id: int,
        event_type: str,
        tenant_id: uuid.UUID | None,
        now: datetime | None = None,
    ) -> bool:
        """Insert the event claim once; duplicates are a no-op."""
        when = now if now is not None else datetime.now(UTC)
        stmt = (
            pg_insert(CHWGapTelemetryEvent)
            .values(
                event_id=event_id,
                chw_id=chw_id,
                event_type=event_type,
                processed_at=when,
                tenant_id=tenant_id,
            )
            .on_conflict_do_nothing(index_elements=[CHWGapTelemetryEvent.event_id])
            .returning(CHWGapTelemetryEvent.event_id)
        )
        res = await self._session.execute(stmt)
        return res.scalar_one_or_none() is not None
