"""Shared Stage A result types and exceptions."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from platform_service.workers.extractors.calibration import CalibrationDecision


@dataclass(frozen=True)
class StageAResult:
    """Summary of one Stage 1 run for a single source document."""

    source_document_id: UUID
    total_pages: int
    pages_persisted: int
    extraction_method_counts: dict[str, int]  # {"text": N, "vision": M, "vision_failed": K}
    calibration: CalibrationDecision
    outline_section_count: int = 0


class Stage1ExtractionError(Exception):
    """Raised by Stage1Extractor.run when the stage cannot meet its success
    contract. Currently used for the vision-recovery tolerance breach
    (`Stage1RecoveryFailedError` subclass). Empty outline used to raise
    here too (pre-c5b6635 outline-partitioner era); under the token-budget
    chunker the outline is supplementary, so empty-outline now logs a
    warning and proceeds. The orchestrator's typed handler at
    pipeline_orchestrator.py:462 still maps this exception to
    `error_jsonb.reason` for the dashboard."""


class Stage1RecoveryFailedError(Stage1ExtractionError):
    """Raised when the vision-recovery pass leaves more than
    `stage_a_vision_failed_tolerance` pages still in `vision_failed` state."""

    def __init__(self, failed_page_numbers: list[int], tolerance: int) -> None:
        self.failed_page_numbers = failed_page_numbers
        self.tolerance = tolerance
        super().__init__(
            f"Stage 1 vision recovery left {len(failed_page_numbers)} pages "
            f"still vision_failed (tolerance={tolerance}): {failed_page_numbers}. "
            f"This usually means Vertex per-project quota was exhausted "
            f"throughout the run. Retry after quota resets, raise the "
            f"per-project RPM limit, or set stage_a_vision_failed_tolerance "
            f"higher if losing these pages is acceptable."
        )
