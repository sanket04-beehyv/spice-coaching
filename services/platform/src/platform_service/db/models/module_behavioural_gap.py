"""Junction: module version ↔ behavioural gaps (many-to-many).

Each module row may link to multiple gaps; at most one link per module is
``is_primary``. ``module.primary_gap_id`` is denormalized from the primary
link for quiz gap-state updates (primary-only).
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class ModuleBehaviouralGap(Base):
    __tablename__ = "module_behavioural_gap"
    __table_args__ = (
        UniqueConstraint(
            "module_id",
            "behavioural_gap_id",
            name="uq_module_behavioural_gap_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module.id", ondelete="CASCADE"),
        nullable=False,
    )
    behavioural_gap_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("behavioural_gap.id", ondelete="CASCADE"),
        nullable=False,
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
