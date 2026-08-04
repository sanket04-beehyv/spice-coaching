"""Daily LLM suggestions for modules to create, from unattributed demand."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from platform_service.db.base import Base


class ModuleCreationSuggestion(Base):
    __tablename__ = "module_creation_suggestion"
    __table_args__ = (
        Index("ix_module_creation_suggestion_tenant_date", "tenant_id", "suggestion_date"),
        Index("ix_module_creation_suggestion_date", "suggestion_date"),
        Index("ix_module_creation_suggestion_matched_module", "matched_module_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    suggestion_date: Mapped[date] = mapped_column(Date, nullable=False)
    # matched_draft | proposed_topic
    suggestion_kind: Mapped[str] = mapped_column(Text, nullable=False)
    matched_module_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module.id", ondelete="SET NULL"),
        nullable=True,
    )
    proposed_topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_title: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    evidence: Mapped[list[ModuleCreationSuggestionEvidence]] = relationship(
        "ModuleCreationSuggestionEvidence",
        back_populates="suggestion",
        cascade="all, delete-orphan",
    )


class ModuleCreationSuggestionEvidence(Base):
    __tablename__ = "module_creation_suggestion_evidence"
    __table_args__ = (Index("ix_module_creation_suggestion_evidence_suggestion", "suggestion_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    suggestion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module_creation_suggestion.id", ondelete="CASCADE"),
        nullable=False,
    )
    # digital_help | module_requested
    source: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sample_event_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_chw_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    suggestion: Mapped[ModuleCreationSuggestion] = relationship(
        "ModuleCreationSuggestion",
        back_populates="evidence",
    )
