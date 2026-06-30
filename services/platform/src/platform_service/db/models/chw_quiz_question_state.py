"""Per-CHW per-quiz-question coaching state (quiz-id telemetry mode).

Used when ``telemetry_behavioural_gap_state_enabled`` is false: quiz outcomes
are tracked per ``module_quiz_question.id`` instead of per behavioural gap.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, PrimaryKeyConstraint, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class CHWQuizQuestionState(Base):
    __tablename__ = "chw_quiz_question_state"
    __table_args__ = (
        PrimaryKeyConstraint("chw_id", "quiz_id", name="pk_chw_quiz_question_state"),
        Index("ix_chw_quiz_question_state_chw_module", "chw_id", "module_id"),
    )

    chw_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module_quiz_question.id", ondelete="CASCADE"),
        nullable=False,
    )
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    failed_attempts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_failed_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_to_supervisor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # active | resolved
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
