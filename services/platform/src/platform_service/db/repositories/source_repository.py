"""W-2 source repository — CRUD for source_document, source_page, content_block.

Async sessions per repository convention. Named methods only (no generic
find_by). Used by Stage A worker (workers/stage_a_extract.py) to persist
extraction results.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.content_block import ContentBlock
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage


def _escape_ilike_pattern(value: str) -> str:
    """Escape SQL ``LIKE``/``ILIKE`` wildcards in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


SOURCE_DOCUMENT_SORT_KEYS = frozenset(
    {
        "ingested_at",
        "title",
        "source_type",
        "status",
        "content_domain",
        "original_filename",
    }
)
SOURCE_DOCUMENT_SORT_DIRS = frozenset({"asc", "desc"})
DEFAULT_SOURCE_DOCUMENT_SORT_BY = "ingested_at"
DEFAULT_SOURCE_DOCUMENT_SORT_DIR = "desc"


def _nullable_text_order(column, *, descending: bool):
    if descending:
        return column.desc().nullslast()
    return column.asc().nullslast()


def _source_document_order_clauses(sort_by: str, sort_dir: str) -> list[Any]:
    descending = sort_dir == "desc"
    order_fn = (lambda col: col.desc()) if descending else (lambda col: col.asc())

    if sort_by == "ingested_at":
        primary = order_fn(SourceDocument.ingested_at)
    elif sort_by == "title":
        primary = order_fn(SourceDocument.title)
    elif sort_by == "source_type":
        primary = order_fn(SourceDocument.source_type)
    elif sort_by == "status":
        primary = order_fn(SourceDocument.status)
    elif sort_by == "content_domain":
        primary = order_fn(SourceDocument.content_domain)
    elif sort_by == "original_filename":
        primary = _nullable_text_order(SourceDocument.original_filename, descending=descending)
    else:
        raise ValueError(f"unsupported sort_by: {sort_by}")

    return [primary, order_fn(SourceDocument.id)]


class SourceRepository:
    """CRUD for the v3.3 source layer (source_document → source_page → content_block)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── source_document ──────────────────────────────────────────────────

    async def create_source_document(
        self,
        *,
        title: str,
        source_type: str,
        primary_language: str,
        content_domain: str,
        original_storage_path: str,
        source_document_family_id: UUID | None = None,
        version_label: str | None = None,
        publication_date: date | None = None,
        ingested_by: UUID | None = None,
        content_sha256: str | None = None,
        original_filename: str | None = None,
        uploaded_by: str | None = None,
        description: str | None = None,
        sync_published_visible: bool = False,
        status: str = "ingesting",
    ) -> SourceDocument:
        """Insert a new source_document and return the persisted row."""
        doc = SourceDocument(
            title=title,
            source_type=source_type,
            primary_language=primary_language,
            content_domain=content_domain,
            original_storage_path=original_storage_path,
            source_document_family_id=source_document_family_id or uuid4(),
            version_label=version_label,
            publication_date=publication_date,
            ingested_by=ingested_by,
            content_sha256=content_sha256,
            original_filename=original_filename,
            uploaded_by=uploaded_by,
            description=description,
            sync_published_visible=sync_published_visible,
            status=status,
        )
        self._session.add(doc)
        await self._session.flush()
        return doc

    async def get_source_document(self, document_id: UUID) -> SourceDocument | None:
        result = await self._session.execute(select(SourceDocument).where(SourceDocument.id == document_id))
        return result.scalar_one_or_none()

    async def list_duplicate_candidates_by_content_sha256(
        self,
        content_sha256: str,
    ) -> list[SourceDocument]:
        """Return uploaded/ingested docs with the same content hash (newest first).

        ``failed`` and ``ingesting`` rows do not block re-upload.
        """
        result = await self._session.execute(
            select(SourceDocument)
            .where(
                SourceDocument.content_sha256 == content_sha256,
                SourceDocument.status.in_(("uploaded", "ingested")),
            )
            .order_by(SourceDocument.ingested_at.desc())
        )
        return list(result.scalars().all())

    def _source_documents_filtered_stmt(
        self,
        *,
        statuses: list[str] | None = None,
        source_types: list[str] | None = None,
        filename_query: str | None = None,
        sync_published_visible: bool | None = None,
    ) -> Select[tuple[SourceDocument]]:
        """Shared filter tree for ``list_source_documents`` / ``count_source_documents``."""
        stmt = select(SourceDocument)
        if statuses:
            stmt = stmt.where(SourceDocument.status.in_(statuses))
        else:
            stmt = stmt.where(SourceDocument.status != "retired")
        if source_types:
            stmt = stmt.where(SourceDocument.source_type.in_(source_types))
        if filename_query:
            escaped = _escape_ilike_pattern(filename_query.strip())
            pattern = f"%{escaped}%"
            stmt = stmt.where(
                or_(
                    SourceDocument.original_filename.ilike(pattern, escape="\\"),
                    SourceDocument.title.ilike(pattern, escape="\\"),
                )
            )
        if sync_published_visible is not None:
            stmt = stmt.where(SourceDocument.sync_published_visible.is_(sync_published_visible))
        return stmt

    async def count_source_documents(
        self,
        *,
        statuses: list[str] | None = None,
        source_types: list[str] | None = None,
        filename_query: str | None = None,
        sync_published_visible: bool | None = None,
    ) -> int:
        """Count source documents matching the same filters as ``list_source_documents``."""
        base = self._source_documents_filtered_stmt(
            statuses=statuses,
            source_types=source_types,
            filename_query=filename_query,
            sync_published_visible=sync_published_visible,
        )
        count_stmt = select(func.count()).select_from(
            base.with_only_columns(SourceDocument.id, maintain_column_froms=True).subquery()
        )
        result = await self._session.execute(count_stmt)
        return int(result.scalar_one())

    async def list_source_documents(
        self,
        *,
        statuses: list[str] | None = None,
        source_types: list[str] | None = None,
        filename_query: str | None = None,
        sync_published_visible: bool | None = None,
        sort_by: str = DEFAULT_SOURCE_DOCUMENT_SORT_BY,
        sort_dir: str = DEFAULT_SOURCE_DOCUMENT_SORT_DIR,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SourceDocument]:
        """Return source documents for admin catalog views (newest ingest first by default)."""
        stmt = (
            self._source_documents_filtered_stmt(
                statuses=statuses,
                source_types=source_types,
                filename_query=filename_query,
                sync_published_visible=sync_published_visible,
            )
            .order_by(*_source_document_order_clauses(sort_by, sort_dir))
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_source_documents_by_ids(self, document_ids: list[UUID]) -> list[SourceDocument]:
        """Return all source_document rows whose id is in ``document_ids`` (order not preserved)."""
        if not document_ids:
            return []
        result = await self._session.execute(
            select(SourceDocument).where(SourceDocument.id.in_(document_ids))
        )
        return list(result.scalars().all())

    async def list_sync_published_visible_documents(
        self,
        *,
        domain: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[SourceDocument]:
        """Return documents flagged for device published sync, newest first.

        Excludes soft-deleted knowledge documents (``status='retired'``).
        """
        stmt = select(SourceDocument).where(
            SourceDocument.sync_published_visible.is_(True),
            SourceDocument.status != "retired",
        )
        if domain is not None:
            stmt = stmt.where(SourceDocument.content_domain == domain)
        stmt = stmt.order_by(SourceDocument.ingested_at.desc()).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def clone_for_reingest(
        self, source: SourceDocument, *, uploaded_by: str | None = None
    ) -> SourceDocument:
        """Create a new source_document row pointing at the same stored bytes."""
        return await self.create_source_document(
            title=source.title,
            source_type=source.source_type,
            primary_language=source.primary_language,
            content_domain=source.content_domain,
            original_storage_path=source.original_storage_path,
            source_document_family_id=source.source_document_family_id,
            version_label=source.version_label,
            publication_date=source.publication_date,
            content_sha256=source.content_sha256,
            original_filename=source.original_filename,
            uploaded_by=uploaded_by or source.uploaded_by,
            sync_published_visible=source.sync_published_visible,
            status="uploaded",
        )

    async def update_status(
        self, document_id: UUID, status: str, *, calibration: dict[str, Any] | None = None
    ) -> None:
        """Update status and (optionally) the extraction calibration result."""
        doc = await self.get_source_document(document_id)
        if doc is None:
            raise ValueError(f"source_document {document_id} not found")
        doc.status = status
        if calibration is not None:
            doc.extraction_calibration_jsonb = calibration
        await self._session.flush()

    async def update_outline(
        self,
        document_id: UUID,
        *,
        outline_method: str,
        outline_jsonb: dict[str, Any] | None,
    ) -> None:
        doc = await self.get_source_document(document_id)
        if doc is None:
            raise ValueError(f"source_document {document_id} not found")
        doc.outline_method = outline_method
        doc.outline_jsonb = outline_jsonb
        await self._session.flush()

    async def update_thumbnail_storage_path(self, document_id: UUID, thumbnail_storage_path: str) -> None:
        doc = await self.get_source_document(document_id)
        if doc is None:
            raise ValueError(f"source_document {document_id} not found")
        doc.thumbnail_storage_path = thumbnail_storage_path
        await self._session.flush()

    async def clear_thumbnail_storage_path(self, document_id: UUID) -> None:
        doc = await self.get_source_document(document_id)
        if doc is None:
            raise ValueError(f"source_document {document_id} not found")
        doc.thumbnail_storage_path = None
        await self._session.flush()

    async def clear_extract_artifacts(self, document_id: UUID) -> int:
        """Delete pages (and cascaded content blocks) and clear outline for retry."""
        doc = await self.get_source_document(document_id)
        if doc is None:
            raise ValueError(f"source_document {document_id} not found")
        result = await self._session.execute(
            delete(SourcePage).where(SourcePage.source_document_id == document_id)
        )
        doc.outline_method = None
        doc.outline_jsonb = None
        doc.extraction_calibration_jsonb = None
        doc.status = "ingesting"
        await self._session.flush()
        return int(result.rowcount or 0)

    async def update_metadata(
        self,
        document_id: UUID,
        *,
        title: str | None = None,
        description: str | None = None,
        update_description: bool = False,
    ) -> SourceDocument | None:
        """Update title and/or description without touching ingest status."""
        doc = await self.get_source_document(document_id)
        if doc is None:
            return None
        if title is not None:
            doc.title = title
        if update_description:
            doc.description = description
        await self._session.flush()
        return doc

    # ── source_page ──────────────────────────────────────────────────────

    async def create_source_page(
        self,
        *,
        source_document_id: UUID,
        page_number: int,
        markdown_content: str,
        extraction_method: str,
        extraction_quality_score: float,
        page_image_path: str | None = None,
        text_extraction_alt: str | None = None,
        language_detected: str | None = None,
        metadata_jsonb: dict[str, Any] | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> SourcePage:
        page = SourcePage(
            source_document_id=source_document_id,
            page_number=page_number,
            markdown_content=markdown_content,
            extraction_method=extraction_method,
            extraction_quality_score=extraction_quality_score,
            page_image_path=page_image_path,
            text_extraction_alt=text_extraction_alt,
            language_detected=language_detected,
            metadata_jsonb=metadata_jsonb,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        self._session.add(page)
        await self._session.flush()
        return page

    async def list_pages_for_document(self, document_id: UUID) -> list[SourcePage]:
        result = await self._session.execute(
            select(SourcePage)
            .where(SourcePage.source_document_id == document_id)
            .order_by(SourcePage.page_number)
        )
        return list(result.scalars().all())

    async def update_page_image_path(self, page_id: UUID, page_image_path: str) -> None:
        """Lazy-render policy: text-path page image written on first reviewer drill-down."""
        page = await self._session.get(SourcePage, page_id)
        if page is None:
            raise ValueError(f"source_page {page_id} not found")
        page.page_image_path = page_image_path
        await self._session.flush()

    async def update_page_extraction(
        self,
        page_id: UUID,
        *,
        markdown_content: str,
        extraction_method: str,
        extraction_quality_score: float,
        page_image_path: str | None = None,
    ) -> None:
        """Replace a page's extraction output. Used by Stage 1's vision-recovery
        pass to upgrade rows that initially landed as `vision_failed`.
        """
        page = await self._session.get(SourcePage, page_id)
        if page is None:
            raise ValueError(f"source_page {page_id} not found")
        page.markdown_content = markdown_content
        page.extraction_method = extraction_method
        page.extraction_quality_score = extraction_quality_score
        if page_image_path is not None:
            page.page_image_path = page_image_path
        await self._session.flush()

    async def list_vision_failed_pages(self, document_id: UUID) -> list[SourcePage]:
        """Pages whose Stage 1 vision call raised on the main loop. The
        recovery pass retries vision on these; the tolerance check then
        verifies the count is within the configured budget.
        """
        result = await self._session.execute(
            select(SourcePage)
            .where(
                SourcePage.source_document_id == document_id,
                SourcePage.extraction_method == "vision_failed",
            )
            .order_by(SourcePage.page_number)
        )
        return list(result.scalars().all())

    # ── content_block ────────────────────────────────────────────────────

    async def create_content_block(
        self,
        *,
        source_page_id: UUID,
        block_order: int,
        block_type: str,
        content_text: str,
        content_language: str | None = None,
        heading_path_jsonb: list[Any] | None = None,
    ) -> ContentBlock:
        block = ContentBlock(
            source_page_id=source_page_id,
            block_order=block_order,
            block_type=block_type,
            content_text=content_text,
            content_language=content_language,
            heading_path_jsonb=heading_path_jsonb,
        )
        self._session.add(block)
        await self._session.flush()
        return block

    async def bulk_create_content_blocks(self, blocks: list[dict[str, Any]]) -> list[ContentBlock]:
        """Bulk insert for performance when a page has many blocks."""
        rows = [ContentBlock(**b) for b in blocks]
        self._session.add_all(rows)
        await self._session.flush()
        return rows

    async def list_blocks_for_page(self, page_id: UUID) -> list[ContentBlock]:
        result = await self._session.execute(
            select(ContentBlock)
            .where(ContentBlock.source_page_id == page_id)
            .order_by(ContentBlock.block_order)
        )
        return list(result.scalars().all())

    async def list_block_provenance_by_ids(
        self, block_ids: list[UUID]
    ) -> list[tuple[UUID, int, UUID, int | None, int | None]]:
        """Return ``(block_id, page_number, source_document_id, start_ms, end_ms)``
        for each block id, joining ContentBlock → SourcePage.

        ``start_ms`` / ``end_ms`` are populated only for AV chunk pages
        (NULL for PDF/DOCX/PPTX). Used by the RAG attribution surface to
        render either "page 12 of X.pdf" or "10:00–12:00 of X.mp4".
        """
        if not block_ids:
            return []
        result = await self._session.execute(
            select(
                ContentBlock.id,
                SourcePage.page_number,
                SourcePage.source_document_id,
                SourcePage.start_ms,
                SourcePage.end_ms,
            )
            .join(SourcePage, ContentBlock.source_page_id == SourcePage.id)
            .where(ContentBlock.id.in_(block_ids))
        )
        return [(row[0], row[1], row[2], row[3], row[4]) for row in result.all()]
