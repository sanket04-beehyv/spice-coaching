import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class CHWVideoAssignment(Base):
    __tablename__ = "chw_video_assignment"
    __table_args__ = (
        UniqueConstraint("source_document_id", "user_id", name="uq_video_assignment_user"),
        UniqueConstraint("source_document_id", "tenant_id", name="uq_video_assignment_tenant"),
        UniqueConstraint("source_document_id", "upazila", name="uq_video_assignment_upazila"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    assignment_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # "individual", "po_sk", "geographical", "group"
    tenant_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    upazila: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True)
    assigned_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
