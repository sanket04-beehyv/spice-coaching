"""Document source extractor (PDF / PPTX / DOCX).

Wraps the synchronous ``text_extractor.extract_pages`` for the Stage A
orchestrator. Output ``requires_calibration=True`` so the orchestrator
runs the per-page vision-fallback decision after this step.
"""

from collections.abc import Callable
from pathlib import Path

import anyio

from platform_service.workers.extractors.base import SourceExtractionResult, SourceExtractor
from platform_service.workers.extractors.text_extractor import ExtractedPage, extract_pages


class DocumentSourceExtractor(SourceExtractor):
    supported_types: frozenset[str] = frozenset({"pdf", "pptx", "docx"})

    def __init__(
        self,
        extract_pages_fn: Callable[..., list[ExtractedPage]] | None = None,
    ) -> None:
        self._extract_pages_fn = extract_pages_fn or extract_pages

    async def extract(
        self,
        source_path: str | Path,
        *,
        source_type: str,
        primary_language: str,
    ) -> SourceExtractionResult:
        pages = await anyio.to_thread.run_sync(lambda: self._extract_pages_fn(source_path, source_type))
        return SourceExtractionResult(
            pages=pages,
            requires_calibration=True,
            extraction_method_label="text",
        )
