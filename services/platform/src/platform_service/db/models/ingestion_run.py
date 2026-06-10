"""v3.3 ingestion_run + ingestion_run_step — per-pipeline-pass state.

Per Data Model v3.3 §4.1, §4.2. Staging tables (truncatable on retention
schedule, default 30 days). Make pipeline runs restartable, debuggable, and
auditable. Not part of the durable content record.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class IngestionRun(Base):
    __tablename__ = "ingestion_run"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # running | succeeded | failed | partially_succeeded
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    error_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class IngestionRunStep(Base):
    __tablename__ = "ingestion_run_step"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ingestion_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    # vision_extract | outline | module_identify | card_draft | translate | publish
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # pending | running | succeeded | failed | skipped
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    input_summary_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    output_summary_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    error_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
