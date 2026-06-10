"""v3.3 chw_behavioural_gap_state — per-CHW per-behavioural-gap state.

Per Data Model v3.3 §7.1. Named `chw_behavioural_gap_state` (not the spec's
logical name `chw_gap_profile`) because the legacy `chw_gap_profile` table
remains populated during the deprecation window. Once the legacy code path
is removed, this table can be renamed.

Fields per the v3.2 expansion (severity, last_reinforced_at) plus the
quiz-failure escalation tracking added in v3.1.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, PrimaryKeyConstraint, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class CHWBehaviouralGapState(Base):
    __tablename__ = "chw_behavioural_gap_state"
    __table_args__ = (
        PrimaryKeyConstraint("chw_id", "behavioural_gap_id", name="pk_chw_behavioural_gap_state"),
    )

    chw_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    behavioural_gap_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("behavioural_gap.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # low | moderate | high
    severity_current: Mapped[str] = mapped_column(Text, nullable=False, default="moderate")
    first_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When a module addressing this gap was last delivered.
    last_reinforced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Number of times the underlying telemetry pattern was observed; trigger
    # fires when this reaches the per-module threshold (default 2 in 14-day
    # window per Architecture R8).
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Number of consecutive failed quiz attempts on the addressing module;
    # resets on a passed attempt.
    failed_attempts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_failed_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # True after failed_attempts_count exceeds the configured threshold
    # (default ≥ 3 within 30 days per Pipeline §12A); surfaces as a mentoring
    # cue on supervisor dashboard.
    escalated_to_supervisor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # active | monitoring | resolved | suppressed
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
