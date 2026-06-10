"""Stage A vision fallback and recovery pass."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import UUID

from platform_service.config import get_settings
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.workers.extractors.page_renderer import (
    UnsupportedRenderError,
    load_cached_page_png,
    persist_rendered_page_image,
)
from platform_service.workers.extractors.quality_heuristic import QualityScore, score_page
from platform_service.workers.extractors.vision_extractor import (
    VisionExtractionError,
    VisionExtractor,
)

logger = logging.getLogger(__name__)


async def run_vision_path(
    *,
    vision: VisionExtractor,
    render_page,
    source_document_id: UUID,
    source_path: str | Path,
    source_type: str,
    page_number: int,
    text_md: str,
    primary_language: str,
) -> tuple[str, str, str | None, QualityScore]:
    """Render the page → call vision LLM → return (method, markdown, image_path, score)."""
    try:
        png_bytes = await asyncio.to_thread(render_page, source_path, source_type, page_number)
    except (UnsupportedRenderError, Exception) as exc:
        if isinstance(exc, UnsupportedRenderError):
            logger.warning(
                "Vision render unsupported for source_type=%s page=%d; using text fallback",
                source_type,
                page_number,
            )
        else:
            logger.warning(
                "Vision render failed source_document_id=%s page=%d: %s",
                source_document_id,
                page_number,
                exc,
            )
        score = score_page(text_md, primary_language=primary_language)
        return ("vision_failed", text_md, None, score)

    image_path = persist_rendered_page_image(source_document_id, page_number, png_bytes)
    try:
        result = await vision.extract_page(
            page_image_bytes=png_bytes,
            mime_type="image/png",
            page_label=f"{source_document_id}/page_{page_number}",
        )
    except VisionExtractionError as exc:
        logger.warning(
            "Vision extraction failed source_document_id=%s page=%d: %s",
            source_document_id,
            page_number,
            exc,
        )
        score = score_page(text_md, primary_language=primary_language)
        return ("vision_failed", text_md, image_path, score)

    vision_score = score_page(result.markdown, primary_language=primary_language)
    return ("vision", result.markdown, image_path, vision_score)


async def run_vision_recovery_pass(
    *,
    repo: SourceRepository,
    vision: VisionExtractor,
    render_page,
    source_document_id: UUID,
    source_path: str | Path,
    source_type: str,
    primary_language: str,
    method_counts: dict[str, int],
) -> int:
    """Retry vision on pages marked vision_failed; enforce tolerance budget."""
    settings = get_settings()
    failed = await repo.list_vision_failed_pages(source_document_id)
    if not failed:
        return 0

    logger.info(
        "Stage 1 recovery pass: %d pages marked vision_failed; waiting %.0fs before retry",
        len(failed),
        settings.stage_a_vision_recovery_initial_delay_s,
    )
    await asyncio.sleep(settings.stage_a_vision_recovery_initial_delay_s)

    recovered = 0
    for page in failed:
        if await _retry_vision_for_page(
            repo=repo,
            vision=vision,
            render_page=render_page,
            source_document_id=source_document_id,
            source_path=source_path,
            source_type=source_type,
            page_number=page.page_number,
            page_id=page.id,
            primary_language=primary_language,
            max_retries=settings.stage_a_vision_recovery_max_retries,
        ):
            recovered += 1
            method_counts["vision"] = method_counts.get("vision", 0) + 1
            method_counts["vision_failed"] = max(0, method_counts.get("vision_failed", 0) - 1)

    residual = await repo.list_vision_failed_pages(source_document_id)
    residual_count = len(residual)
    logger.info(
        "Stage 1 recovery pass complete: recovered=%d residual_failed=%d tolerance=%d",
        recovered,
        residual_count,
        settings.stage_a_vision_failed_tolerance,
    )
    if residual_count > settings.stage_a_vision_failed_tolerance:
        from platform_service.workers.stage_a_extract import Stage1RecoveryFailedError

        raise Stage1RecoveryFailedError(
            failed_page_numbers=[p.page_number for p in residual],
            tolerance=settings.stage_a_vision_failed_tolerance,
        )
    return recovered


async def _retry_vision_for_page(
    *,
    repo: SourceRepository,
    vision: VisionExtractor,
    render_page,
    source_document_id: UUID,
    source_path: str | Path,
    source_type: str,
    page_number: int,
    page_id: UUID,
    primary_language: str,
    max_retries: int,
) -> bool:
    """Re-render PNG (or load cached), retry vision up to max_retries times."""
    png_bytes = load_cached_page_png(source_document_id, page_number)
    if png_bytes is None:
        try:
            png_bytes = await asyncio.to_thread(render_page, source_path, source_type, page_number)
            persist_rendered_page_image(source_document_id, page_number, png_bytes)
        except Exception as exc:
            logger.warning("Stage 1 recovery: cannot re-render page %d: %s", page_number, exc)
            return False

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            result = await vision.extract_page(
                page_image_bytes=png_bytes,
                mime_type="image/png",
                page_label=f"{source_document_id}/page_{page_number}",
            )
            vision_score = score_page(result.markdown, primary_language=primary_language)
            await repo.update_page_extraction(
                page_id,
                markdown_content=result.markdown,
                extraction_method="vision",
                extraction_quality_score=vision_score.composite_score,
            )
            logger.info(
                "Stage 1 recovery: page %d recovered on attempt %d/%d (content_len=%d)",
                page_number,
                attempt,
                max_retries,
                len(result.markdown),
            )
            return True
        except VisionExtractionError as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = 30.0 * attempt
                logger.warning(
                    "Stage 1 recovery: page %d attempt %d/%d failed: %s — retrying in %.0fs",
                    page_number,
                    attempt,
                    max_retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    logger.warning(
        "Stage 1 recovery: page %d still failed after %d attempts: %s",
        page_number,
        max_retries,
        last_exc,
    )
    return False
