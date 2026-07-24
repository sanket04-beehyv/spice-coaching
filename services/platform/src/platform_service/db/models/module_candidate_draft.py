"""ModuleCandidateDraft — ephemeral Stage 2 pipeline state.

Per `docs/ARCHITECTURE_RESET.md`. The W-6 reviewer queue was deleted; this
table now serves only as a debugging trail for Stage 2 (proposals + their
quality flags) and as the per-candidate retry surface for Stage 2-draft.
There are no claim, expiry, or review-status workflow fields — those were
dropped in migration 0007.
"""

# Pylint false-positive: SQLAlchemy's `func.now()` is valid here.
# pylint: disable=not-callable

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class ModuleCandidateDraft(Base):
    __tablename__ = "module_candidate_draft"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ingestion_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ingestion_run.id", ondelete="CASCADE"),
        nullable=False,
    )
    proposed_title: Mapped[str] = mapped_column(Text, nullable=False)
    # Admin filter domain emitted by Stage C; copied onto module.domain at Stage D.
    domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stage 2 dropped gap context from its prompt (architecture reset);
    # this column is nullable and the pipeline writes None. Reviewer-
    # authored bindings carry the gap mapping post-publish.
    behavioural_gap_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description_localized: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
    # Array of {source_document_id, source_page_id, content_block_ids[]}.
    source_provenance_jsonb: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    estimated_card_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_quiz_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clinical_review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # refresher | content_update | digital_proficiency
    proposed_module_type: Mapped[str] = mapped_column(Text, nullable=False, default="refresher")
    # For content_update candidates: extracted previous/current/rationale summaries.
    previous_practice_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_practice_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When admin ingestion instructions steered Stage C: why this candidate
    # was emitted relative to those instructions.
    ingestion_instruction_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Advisory quality flags from the insufficient-source heuristic.
    # Stage 2-draft propagates these onto `module.quality_flags_jsonb`
    # when the candidate is drafted; they never gate publish.
    quality_flags_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
