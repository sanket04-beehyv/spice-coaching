"""Stage 1 document-level emptiness check after extraction."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.workers.stage_a_types import Stage1DocumentEmptyError


def total_stripped_text_chars(page_markdowns: Iterable[str]) -> int:
    """Sum of stripped character counts across page markdown bodies."""
    return sum(len((md or "").strip()) for md in page_markdowns)


async def assert_document_has_text(
    repo: SourceRepository,
    session: AsyncSession,
    *,
    source_document_id: UUID,
    page_markdowns: Iterable[str],
    total_pages: int,
) -> None:
    """Fail Stage 1 when there are no pages or total stripped text is below threshold.

    Marks ``source_document.status`` as ``failed`` and commits before raising so a
    subsequent orchestrator rollback cannot undo the failure signal.
    """
    settings = get_settings()
    min_chars = settings.extraction_quality_text_empty_min_chars
    total_chars = total_stripped_text_chars(page_markdowns)
    if total_pages == 0 or total_chars < min_chars:
        await repo.update_status(source_document_id, "failed")
        await session.commit()
        raise Stage1DocumentEmptyError()
