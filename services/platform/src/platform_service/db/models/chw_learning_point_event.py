"""One row per scored telemetry event — PK `event_id` gives idempotency; totals are SUM(points)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class CHWLearningPointEvent(Base):
    __tablename__ = "chw_learning_point_event"
    __table_args__ = (Index("ix_chw_learning_point_event_chw_id", "chw_id"),)

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    chw_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    awarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
