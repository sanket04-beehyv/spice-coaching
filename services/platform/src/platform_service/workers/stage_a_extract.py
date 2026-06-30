"""Stage 1 — fused extraction + outline (was Stage A + Stage B).

Per `docs/ARCHITECTURE_RESET.md`. The flow per source document:

1. Count pages (page_renderer.count_pages).
2. Calibrate (sampled per-page text extraction → text_only / all_vision /
   per_page decision).
3. Extract every page (text or vision per the calibration), persisting
   SourcePage rows with markdown_content + extraction_method.
4. **Assemble the outline deterministically** from the per-page markdown
   (heading lines parsed by `markdown_outline_parser`). Persist
   `source_document.outline_jsonb`.
5. Stage 1 success contract: `pages_persisted > 0` AND `section_count > 0`.
   An empty outline fails the stage hard — no separate Stage B, no silent
   `outline_method='failed'` masquerading as success. The orchestrator
   propagates this as a real ingestion_run_step failure so downstream
   stages never run on garbage.

The caller (pipeline_orchestrator) is responsible for:
- Creating the source_document row before invoking us
- Wrapping us in an ingestion_run for failure tracking
- Running Stage 2 (identify+draft) and Stage 3 (publish) afterward
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from uuid import UUID

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.workers.extractors.base import (
    SourceExtractor,
    UnsupportedSourceTypeError,
)
from platform_service.workers.extractors.calibration import build_calibration_decision
from platform_service.workers.extractors.document_extractor import DocumentSourceExtractor
from platform_service.workers.extractors.media_extractor import MediaSourceExtractor
from platform_service.workers.extractors.page_renderer import (
    UnsupportedRenderError,
    count_pages,
    render_page_to_png,
)
from platform_service.workers.extractors.stage_a_document_path import run_document_path
from platform_service.workers.extractors.stage_a_media_path import run_media_transcript_path
from platform_service.workers.extractors.text_extractor import TextExtractionError
from platform_service.workers.extractors.vision_extractor import VisionExtractor
from platform_service.workers.stage_a_types import (
    Stage1ExtractionError,
    Stage1RecoveryFailedError,
    StageAResult,
)

logger = logging.getLogger(__name__)


class StageAExtractor:
    """Stage A orchestrator. One instance per ingestion run; reusable across runs."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        vision_extractor: VisionExtractor | None = None,
        ai_client: AIRuntimeClient | None = None,
        page_renderer=None,
        text_extractor_fn=None,
        media_transcriber_fn=None,
        extractors: dict[str, SourceExtractor] | None = None,
    ) -> None:
        self._session = session
        self._repo = SourceRepository(session)
        self._vision = vision_extractor or VisionExtractor()
        self._render_page = page_renderer or render_page_to_png
        if extractors is None:
            doc_ext = DocumentSourceExtractor(extract_pages_fn=text_extractor_fn)
            media_ext = MediaSourceExtractor(ai_client=ai_client, transcribe_fn=media_transcriber_fn)
            extractors = {t: doc_ext for t in doc_ext.supported_types} | {
                t: media_ext for t in media_ext.supported_types
            }
        self._extractors = extractors

    async def run(
        self,
        *,
        source_document_id: UUID,
        source_path: str | Path,
        source_type: str,
        primary_language: str | None = None,
    ) -> StageAResult:
        """Execute Stage A end-to-end for one source document."""
        resolved_primary_language = primary_language or get_settings().deployment_primary_locale
        total_pages = await self._count_pages_or_fail(source_document_id, source_path, source_type)
        if total_pages == 0:
            return await self._empty_document_result(source_document_id)

        extraction = await self._extract_source_or_fail(
            source_document_id, source_path, source_type, resolved_primary_language
        )
        if not extraction.requires_calibration:
            return await run_media_transcript_path(
                self._repo,
                self._session,
                source_document_id=source_document_id,
                text_pages=extraction.pages,
                total_pages=len(extraction.pages),
                primary_language=resolved_primary_language,
            )
        return await run_document_path(
            self,
            source_document_id=source_document_id,
            source_path=source_path,
            source_type=source_type,
            primary_language=resolved_primary_language,
            text_pages=extraction.pages,
            total_pages=total_pages,
        )

    async def _count_pages_or_fail(
        self, source_document_id: UUID, source_path: str | Path, source_type: str
    ) -> int:
        try:
            return await anyio.to_thread.run_sync(lambda: count_pages(source_path, source_type))
        except (UnsupportedRenderError, FileNotFoundError, Exception) as exc:
            logger.exception(
                "Stage A page count failed source_document_id=%s path=%s",
                source_document_id,
                source_path,
            )
            await self._repo.update_status(source_document_id, "failed")
            raise TextExtractionError(f"Stage A: cannot count pages: {exc}") from exc

    async def _empty_document_result(self, source_document_id: UUID) -> StageAResult:
        calibration = build_calibration_decision(sample_pages=[], sample_pass_count=0, sample_fail_count=0)
        await self._repo.update_status(
            source_document_id,
            status="ingested",
            calibration=calibration.to_jsonb(),
        )
        return StageAResult(
            source_document_id=source_document_id,
            total_pages=0,
            pages_persisted=0,
            extraction_method_counts={},
            calibration=calibration,
        )

    async def _extract_source_or_fail(
        self,
        source_document_id: UUID,
        source_path: str | Path,
        source_type: str,
        primary_language: str,
    ):
        extractor = self._extractors.get(source_type)
        if extractor is None:
            await self._repo.update_status(source_document_id, "failed")
            raise UnsupportedSourceTypeError(
                f"no Stage A extractor registered for source_type={source_type!r}"
            )
        try:
            return await extractor.extract(
                source_path,
                source_type=source_type,
                primary_language=primary_language,
            )
        except TextExtractionError:
            await self._repo.update_status(source_document_id, "failed")
            raise


__all__ = ["StageAExtractor", "StageAResult", "Stage1ExtractionError", "Stage1RecoveryFailedError"]


_ = uuid
