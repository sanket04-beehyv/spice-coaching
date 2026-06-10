"""v3.3 content_block — semantic unit within a SourcePage.

Per Data Model v3.3 §3.3. Citation primitive every downstream artefact (cards,
quiz questions, snippets) references. heading_path_jsonb carries the inherited
heading hierarchy at this block (used by Stage C corpus identification).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class ContentBlock(Base):
    __tablename__ = "content_block"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_page.id", ondelete="CASCADE"),
        nullable=False,
    )
    block_order: Mapped[int] = mapped_column(Integer, nullable=False)
    # heading | paragraph | list | table | figure | callout
    block_type: Mapped[str] = mapped_column(Text, nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_language: Mapped[str | None] = mapped_column(Text, nullable=True)
    heading_path_jsonb: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
