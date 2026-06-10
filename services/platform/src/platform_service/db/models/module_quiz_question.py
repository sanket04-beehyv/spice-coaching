"""ModuleQuizQuestion — quiz item linked directly to one module version.

Per `docs/ARCHITECTURE_RESET.md`:

- Cards live inline on `module.module_json`; quiz questions stay relational
  because `chw_quiz_attempt` rows carry FKs into them and per-question
  analytics joins need real columns.
- `module_id` is a direct FK (the membership join table was dropped in
  migration 0007 — every question belongs to exactly one module version).
- `question_order` carries display order within the module.
- `case_setup_*` fields support scenario-based quiz format (present patient
  case → CHW makes decision → system explains).
- `distractor_critique_jsonb` carries per-distractor quality scores from
  the post-publish quiz worker's critique pass.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class ModuleQuizQuestion(Base):
    __tablename__ = "module_quiz_question"
    __table_args__ = (
        UniqueConstraint(
            "question_family_id",
            "question_version",
            name="uq_module_quiz_family_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Direct FK to the parent module version (replaces the dropped
    # module_quiz_question_membership join table). Nullable in the schema
    # for migration headroom; in practice every persisted row has a
    # module_id set by the quiz-generation worker.
    module_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    question_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    question_family_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    question_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Scenario presented to the CHW (e.g. "PW, 22 weeks, BP 145/95, edema present").
    # Optional; biased toward populated by drafter prompt.
    case_setup_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_setup_bn: Mapped[str | None] = mapped_column(Text, nullable=True)

    question_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_bn: Mapped[str] = mapped_column(Text, nullable=False)

    # single_select | multi_select | order_sequence
    question_type: Mapped[str] = mapped_column(Text, nullable=False, default="single_select")

    options_en: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    options_bn: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    # One element for single_select, multiple for multi_select, ordered for sequence.
    correct_indices: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)

    explanation_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation_bn: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which card family this question primarily tests. References
    # card_family_id (not card_version) so the question stays valid across
    # card edits.
    primary_card_family_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_block_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=True)

    # easy | moderate | hard
    difficulty: Mapped[str] = mapped_column(Text, nullable=False, default="moderate")

    # Per-distractor source-traceability scores from Stage D critique pass:
    # { "0": {"score": 0.7, "passed": true, "regenerated": false}, "1": {...}, ... }
    distractor_critique_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    field_flags_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
