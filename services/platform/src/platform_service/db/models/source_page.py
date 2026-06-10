"""v3.3 source_page — one extracted page of a SourceDocument.

Per Data Model v3.3 §3.2. Single canonical markdown_content (sourced from text
extraction or vision LLM, recorded in extraction_method). text_extraction_alt
populated only on divergence between text and vision paths. page_image_path
nullable: vision-path always populated; text-path lazy-rendered on reviewer
drill-down (Pipeline §4.5).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class SourcePage(Base):
    __tablename__ = "source_page"
    __table_args__ = (UniqueConstraint("source_document_id", "page_number", name="uq_source_page_doc_page"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("source_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # AV chunk boundaries from the server-side splitter (milliseconds).
    # NULL for PDF/DOCX/PPTX pages. Owned by the splitter, never by the LLM.
    start_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Object-storage reference to rendered PNG at 2× DPI. NULL for text-path
    # pages until the reviewer first opens the page (lazy-render policy).
    page_image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Canonical language-preserved page content; sourced per `extraction_method`.
    markdown_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # `text` | `vision` — which path produced markdown_content.
    extraction_method: Mapped[str] = mapped_column(Text, nullable=False, default="text")
    # Quality heuristic score on the text-extracted output. Recorded even when
    # vision was used so tuning can revisit the threshold later.
    extraction_quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Populated only when both text and vision were run AND results diverge.
    text_extraction_alt: Mapped[str | None] = mapped_column(Text, nullable=True)
    language_detected: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Heading path inferred at page level, key terms, tags.
    metadata_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
