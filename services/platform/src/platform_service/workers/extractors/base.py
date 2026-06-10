"""Stage 1 source-extractor protocol.

Defines the contract every source-type extractor implements. The Stage A
orchestrator looks up the right extractor by ``source_type`` and delegates
the "raw source → per-page markdown" step. Per-page vision fallback is
orchestrated separately by Stage A after calibration.

Adding a new source type (e.g., SRT subtitles, screen recordings) becomes
a single extractor implementation + registry entry, with no changes to
the orchestrator's control flow.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from platform_service.workers.extractors.text_extractor import ExtractedPage


@dataclass(frozen=True)
class SourceExtractionResult:
    """Output of one source-extractor invocation.

    ``requires_calibration`` is True for document sources whose output
    may need per-page vision fallback (PDF/Office); False for transcript
    sources that the Stage A orchestrator persists directly.

    ``extraction_method_label`` is the string recorded on each
    ``source_page.extraction_method`` row for downstream audit.
    """

    pages: list[ExtractedPage]
    requires_calibration: bool
    extraction_method_label: str


@runtime_checkable
class SourceExtractor(Protocol):
    """Convert a stored source into per-page markdown."""

    supported_types: frozenset[str]

    async def extract(
        self,
        source_path: str | Path,
        *,
        source_type: str,
        primary_language: str,
    ) -> SourceExtractionResult: ...


class UnsupportedSourceTypeError(ValueError):
    """Raised when no extractor is registered for the requested source_type."""
