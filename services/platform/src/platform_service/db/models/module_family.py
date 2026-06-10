"""v3.3 module_family — stable identifier for a logical module across versions.

Per Data Model v3.3 §5.1. Every reviewer-approved version of the same logical
module shares this `module_family_id`. `current_published_module_id` is updated
on each new published version; older versions remain queryable.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class ModuleFamily(Base):
    __tablename__ = "module_family"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Human-readable code, e.g. RMNCH-ANC-REFERRAL
    module_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Pointer to the live version. Nullable because a family is created before
    # its first version is published.
    current_published_module_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
