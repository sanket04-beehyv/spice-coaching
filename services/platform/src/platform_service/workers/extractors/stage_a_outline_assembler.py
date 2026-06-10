"""Stage A outline assembly — deterministic heading parse over persisted pages."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.workers.extractors.markdown_outline_parser import parse_outline

logger = logging.getLogger(__name__)


async def assemble_outline_from_page_pairs(
    repo: SourceRepository,
    session: AsyncSession,
    *,
    source_document_id: UUID,
    page_pairs: list[tuple[int, str]],
    total_pages: int,
    primary_language: str,
    empty_outline_log: str | None = None,
) -> int:
    """Parse heading markers from in-memory page pairs, persist outline_jsonb."""
    parsed = parse_outline(
        page_pairs,
        total_pages=total_pages,
        primary_language=primary_language,
    )
    section_count = len(parsed.sections)
    await repo.update_outline(
        source_document_id,
        outline_method="markdown_parser",
        outline_jsonb=parsed.to_jsonb(),
    )
    await session.commit()

    if section_count == 0 and empty_outline_log:
        logger.warning(empty_outline_log, source_document_id)

    return section_count


async def assemble_outline_from_persisted_pages(
    repo: SourceRepository,
    session: AsyncSession,
    *,
    source_document_id: UUID,
    total_pages: int,
    primary_language: str,
    pages_persisted: int,
) -> int:
    """Read committed source_page rows, build outline_jsonb, commit."""
    pages_rows = await repo.list_pages_for_document(source_document_id)
    page_pairs = [(p.page_number, p.markdown_content or "") for p in pages_rows]
    parsed = parse_outline(
        page_pairs,
        total_pages=total_pages,
        primary_language=primary_language,
    )
    section_count = len(parsed.sections)
    await repo.update_outline(
        source_document_id,
        outline_method="markdown_parser",
        outline_jsonb=parsed.to_jsonb(),
    )
    await session.commit()

    if section_count == 0:
        logger.warning(
            "Stage 1 outline empty source_document_id=%s pages=%d — "
            "identifier will run on body content only (no boundary hints)",
            source_document_id,
            pages_persisted,
        )

    return section_count
