"""Stage A PDF/PPTX/DOCX path — calibration, per-page extract, recovery, outline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from platform_service.config import get_settings
from platform_service.workers.extractors.calibration import sample_calibration_for_document
from platform_service.workers.extractors.extraction_markdown import persist_markdown_content
from platform_service.workers.extractors.quality_heuristic import score_page
from platform_service.workers.extractors.stage_a_outline_assembler import (
    assemble_outline_from_persisted_pages,
)
from platform_service.workers.extractors.stage_a_vision_recovery import (
    run_vision_path,
    run_vision_recovery_pass,
)
from platform_service.workers.extractors.text_extractor import ExtractedPage
from platform_service.workers.stage_a_types import StageAResult

if TYPE_CHECKING:
    from platform_service.workers.stage_a_extract import StageAExtractor

logger = logging.getLogger(__name__)


async def _persist_page(
    host: StageAExtractor,
    *,
    source_document_id: UUID,
    page_number: int,
    method: str,
    markdown: str,
    score,
    image_path: str | None,
    primary_language: str,
    method_counts: dict[str, int],
) -> int:
    await host._repo.create_source_page(
        source_document_id=source_document_id,
        page_number=page_number,
        markdown_content=persist_markdown_content(markdown),
        extraction_method=method,
        extraction_quality_score=score.composite_score,
        page_image_path=image_path,
        language_detected=primary_language,
    )
    method_counts[method] = method_counts.get(method, 0) + 1
    return 1


async def run_document_path(
    host: StageAExtractor,
    *,
    source_document_id: UUID,
    source_path: str | Path,
    source_type: str,
    primary_language: str,
    text_pages: list[ExtractedPage],
    total_pages: int,
) -> StageAResult:
    """Execute calibration, per-page processing, vision recovery, and outline assembly."""
    text_by_page = {p.page_number: p for p in text_pages}
    settings = get_settings()
    calibration, sampled_scores = sample_calibration_for_document(
        text_by_page,
        total_pages=total_pages,
        primary_language=primary_language,
        sample_size=settings.extraction_calibration_sample_size,
    )
    logger.info(
        "Stage A calibration source_document_id=%s path=%s pass=%d fail=%d → %s",
        source_document_id,
        source_path,
        calibration.sample_pass_count,
        calibration.sample_fail_count,
        calibration.path,
    )

    method_counts: dict[str, int] = {}
    pages_persisted = 0
    vision_kwargs = {
        "vision": host._vision,
        "render_page": host._render_page,
        "source_document_id": source_document_id,
        "source_path": source_path,
        "source_type": source_type,
        "primary_language": primary_language,
    }

    for pn in range(1, total_pages + 1):
        text_md = text_by_page[pn].markdown if pn in text_by_page else ""
        if calibration.path == "all_vision":
            method, markdown, image_path, score = await run_vision_path(
                page_number=pn, text_md=text_md, **vision_kwargs
            )
        elif calibration.path == "text_only":
            score = score_page(
                text_md, primary_language=primary_language, is_multi_page_document=total_pages > 1
            )
            method, markdown, image_path = "text", text_md, None
        else:
            score = sampled_scores.get(pn) or score_page(
                text_md, primary_language=primary_language, is_multi_page_document=total_pages > 1
            )
            if score.passed:
                method, markdown, image_path = "text", text_md, None
            else:
                method, markdown, image_path, score = await run_vision_path(
                    page_number=pn, text_md=text_md, **vision_kwargs
                )

        pages_persisted += await _persist_page(
            host,
            source_document_id=source_document_id,
            page_number=pn,
            method=method,
            markdown=markdown,
            score=score,
            image_path=image_path,
            primary_language=primary_language,
            method_counts=method_counts,
        )

    await host._repo.update_status(source_document_id, status="ingested", calibration=calibration.to_jsonb())
    await host._session.commit()

    recovered = await run_vision_recovery_pass(
        repo=host._repo,
        vision=host._vision,
        render_page=host._render_page,
        source_document_id=source_document_id,
        source_path=source_path,
        source_type=source_type,
        primary_language=primary_language,
        method_counts=method_counts,
    )
    if recovered:
        await host._session.commit()

    section_count = await assemble_outline_from_persisted_pages(
        host._repo,
        host._session,
        source_document_id=source_document_id,
        total_pages=total_pages,
        primary_language=primary_language,
        pages_persisted=pages_persisted,
    )
    logger.info(
        "Stage 1 complete source_document_id=%s pages=%d sections=%d methods=%s",
        source_document_id,
        pages_persisted,
        section_count,
        method_counts,
    )

    return StageAResult(
        source_document_id=source_document_id,
        total_pages=total_pages,
        pages_persisted=pages_persisted,
        extraction_method_counts=method_counts,
        calibration=calibration,
        outline_section_count=section_count,
    )
