"""v3.3 chw_module_completion — per-CHW per-module-family completion state.

Per Data Model v3.3 §7.2. Drives the morning rotation logic, the periodic
refresh after a passed quiz (default 90 days), and the repeated-failure
escalation rule (≥3 failed attempts within 30 days → supervisor alert).
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class CHWModuleCompletion(Base):
    __tablename__ = "chw_module_completion"
    __table_args__ = (PrimaryKeyConstraint("chw_id", "module_family_id", name="pk_chw_module_completion"),)

    chw_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    module_family_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module_family.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Specific version completed (i.e. the version that produced the latest
    # passed attempt). NULL until first pass.
    latest_completed_module_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Specific version of the most recent attempt (which may not have passed).
    latest_attempt_module_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Set on first passed attempt.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Updated on every quiz submission.
    latest_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 0.0–1.0
    latest_quiz_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Score met the configured pass threshold (default 70%).
    latest_attempt_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Resets to 0 on each passed attempt; drives repeated-failure escalation.
    attempts_since_last_pass: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Next periodic refresh after a passed attempt (default +90 days, configurable).
    # Module re-enters morning rotation when this date is reached.
    reinforcement_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
