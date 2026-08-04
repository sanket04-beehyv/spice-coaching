"""Durable admin ingest batch — groups per-source ingestion_runs for polling."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class IngestBatch(Base):
    __tablename__ = "ingest_batch"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # queued | running | succeeded | failed | partially_succeeded
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    # with_quiz | read_only — read_only skips post-publish quiz generation
    assessment_mode: Mapped[str] = mapped_column(Text, nullable=False, default="with_quiz")
    # Optional admin steering text for Stage C module identification (sanitized at start).
    ingestion_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional fixed card/quiz counts per module (null = deployment defaults).
    cards_per_module: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quizzes_per_module: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
