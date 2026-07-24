"""Stage A audio/video transcript path — skips document calibration."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.workers.extractors.calibration import CalibrationDecision
from platform_service.workers.extractors.extraction_markdown import persist_markdown_content
from platform_service.workers.extractors.stage_a_outline_assembler import assemble_outline_from_page_pairs
from platform_service.workers.extractors.text_extractor import ExtractedPage
from platform_service.workers.stage_a_types import StageAResult

logger = logging.getLogger(__name__)


async def run_media_transcript_path(
    repo: SourceRepository,
    session: AsyncSession,
    *,
    source_document_id: UUID,
    text_pages: list[ExtractedPage],
    total_pages: int,
    primary_language: str,
) -> StageAResult:
    """Persist transcript markdown directly, without document-text calibration."""
    calibration = CalibrationDecision(
        path="media_transcript",
        sample_pages_evaluated=[],
        sample_pass_count=0,
        sample_fail_count=0,
        sample_fail_rate=0.0,
    )
    method_counts = {"transcript": 0}
    pages_persisted = 0
    for page in text_pages:
        await repo.create_source_page(
            source_document_id=source_document_id,
            page_number=page.page_number,
            markdown_content=persist_markdown_content(page.markdown),
            extraction_method="transcript",
            extraction_quality_score=(
                page.extraction_quality_score if page.extraction_quality_score is not None else 0.85
            ),
            page_image_path=None,
            language_detected=page.language_detected or primary_language,
            start_ms=page.start_ms,
            end_ms=page.end_ms,
        )
        method_counts["transcript"] += 1
        pages_persisted += 1

    await repo.update_status(
        source_document_id,
        status="ingested",
        calibration=calibration.to_jsonb(),
    )

    page_pairs = [(p.page_number, persist_markdown_content(p.markdown)) for p in text_pages]
    section_count = await assemble_outline_from_page_pairs(
        repo,
        session,
        source_document_id=source_document_id,
        page_pairs=page_pairs,
        total_pages=total_pages,
        primary_language=primary_language,
        empty_outline_log=(
            "Stage 1 media transcript produced no outline sections for source_document_id=%s; proceeding"
        ),
    )

    return StageAResult(
        source_document_id=source_document_id,
        total_pages=total_pages,
        pages_persisted=pages_persisted,
        extraction_method_counts=method_counts,
        calibration=calibration,
        outline_section_count=section_count,
    )
