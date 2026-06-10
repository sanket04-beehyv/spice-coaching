"""Idempotency ledger for gap-state updates driven by telemetry events."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class CHWGapTelemetryEvent(Base):
    """One row per telemetry event that mutates ``chw_behavioural_gap_state``.

    Primary key ``event_id`` prevents duplicate gap observations when Redis
    dedup TTL expires or Celery redelivers a job.
    """

    __tablename__ = "chw_gap_telemetry_event"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    chw_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
