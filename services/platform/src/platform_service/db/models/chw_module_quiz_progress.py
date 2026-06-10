"""Per-question module quiz progress per CHW.

Tracks which quiz questions (``module_quiz_question.id``) a CHW has attempted
for a given module version (``module.id``), regardless of answer correctness.
When the CHW has an attempt record for every question in the module, the module
can be marked completed in ``chw_module_completion`` without a separate
``module_completed`` telemetry event.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, PrimaryKeyConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class CHWModuleQuizProgress(Base):
    __tablename__ = "chw_module_quiz_progress"
    __table_args__ = (
        PrimaryKeyConstraint(
            "chw_id",
            "module_id",
            "quiz_id",
            name="pk_chw_module_quiz_progress",
        ),
        Index("ix_chw_module_quiz_progress_chw_module", "chw_id", "module_id"),
    )

    chw_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module.id", ondelete="CASCADE"),
        nullable=False,
    )
    quiz_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module_quiz_question.id", ondelete="CASCADE"),
        nullable=False,
    )

    first_correct_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
