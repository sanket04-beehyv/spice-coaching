"""W-2 Stage A — empirical extraction calibration.

Per Pipeline v3.3 §4.4. At ingestion start, run the quality heuristic on a
stratified sample of pages and decide the corpus-level extraction path:

- > force_vision_threshold sample fail rate → all pages run vision
  (skip text extraction to save time)
- < skip_vision_threshold sample fail rate → text-only path; vision is only
  invoked on individual page failures
- between thresholds → per-page evaluation (try text, fall back per page)

The decision plus the sample evaluation is recorded on
`source_document.extraction_calibration_jsonb`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from platform_service.config import get_settings
from platform_service.workers.extractors.quality_heuristic import QualityScore, score_page
from platform_service.workers.extractors.text_extractor import ExtractedPage


@dataclass(frozen=True)
class CalibrationDecision:
    """Outcome of the per-document calibration sampling."""

    path: str  # "all_vision" | "text_only" | "per_page"
    sample_pages_evaluated: list[int]
    sample_pass_count: int
    sample_fail_count: int
    sample_fail_rate: float

    def to_jsonb(self) -> dict[str, Any]:
        """Shape stored on source_document.extraction_calibration_jsonb."""
        return {
            "path": self.path,
            "sample_pages_evaluated": list(self.sample_pages_evaluated),
            "sample_pass_count": self.sample_pass_count,
            "sample_fail_count": self.sample_fail_count,
            "sample_fail_rate": self.sample_fail_rate,
            "decision_at": datetime.now(UTC).isoformat(),
        }


def stratified_sample_indices(total_pages: int, sample_size: int) -> list[int]:
    """Pick `sample_size` page indices spread evenly across the document.

    1-indexed page numbers (matching SourcePage.page_number convention).

    Edge cases:
    - total_pages <= sample_size → return every page
    - total_pages == 0 → return []
    - sample_size <= 0 → return []
    """
    if total_pages <= 0 or sample_size <= 0:
        return []
    if total_pages <= sample_size:
        return list(range(1, total_pages + 1))
    # Even spread including first and last page so endpoints get sampled.
    step = (total_pages - 1) / (sample_size - 1) if sample_size > 1 else total_pages
    indices = sorted({1 + int(round(i * step)) for i in range(sample_size)})
    # round() can collide; ensure we still return sample_size unique pages.
    while len(indices) < sample_size:
        # Fill in gaps from the middle outward.
        remaining = [p for p in range(1, total_pages + 1) if p not in indices]
        if not remaining:
            break
        indices.append(remaining[len(remaining) // 2])
        indices.sort()
    return indices[:sample_size]


def decide_path(*, sample_pass_count: int, sample_fail_count: int) -> str:
    """Apply the calibration thresholds to decide the corpus-level path.

    Per Pipeline v3.3 §4.4:
    - fail_rate > force_vision_threshold → "all_vision"
    - fail_rate < skip_vision_threshold  → "text_only"
    - between → "per_page"
    """
    settings = get_settings()
    total = sample_pass_count + sample_fail_count
    if total == 0:
        # No sample evaluated (zero-page doc); default to text_only — Stage A
        # orchestrator will short-circuit on empty page list anyway.
        return "text_only"
    fail_rate = sample_fail_count / total
    if fail_rate > settings.extraction_calibration_force_vision_threshold:
        return "all_vision"
    if fail_rate < settings.extraction_calibration_skip_vision_threshold:
        return "text_only"
    return "per_page"


def sample_calibration_for_document(
    text_by_page: dict[int, ExtractedPage],
    *,
    total_pages: int,
    primary_language: str,
    sample_size: int,
) -> tuple[CalibrationDecision, dict[int, QualityScore]]:
    """Score a stratified page sample and return the calibration decision."""
    sample_pages = stratified_sample_indices(total_pages, sample_size)
    sample_pass = 0
    sample_fail = 0
    sampled_scores: dict[int, QualityScore] = {}
    for pn in sample_pages:
        page = text_by_page.get(pn)
        markdown = page.markdown if page else ""
        score = score_page(
            markdown,
            primary_language=primary_language,
            is_multi_page_document=total_pages > 1,
        )
        sampled_scores[pn] = score
        if score.passed:
            sample_pass += 1
        else:
            sample_fail += 1
    calibration = build_calibration_decision(
        sample_pages=sample_pages,
        sample_pass_count=sample_pass,
        sample_fail_count=sample_fail,
    )
    return calibration, sampled_scores


def build_calibration_decision(
    *,
    sample_pages: list[int],
    sample_pass_count: int,
    sample_fail_count: int,
) -> CalibrationDecision:
    """Assemble a CalibrationDecision from sample-pass counts."""
    total = sample_pass_count + sample_fail_count
    fail_rate = sample_fail_count / total if total else 0.0
    return CalibrationDecision(
        path=decide_path(sample_pass_count=sample_pass_count, sample_fail_count=sample_fail_count),
        sample_pages_evaluated=sample_pages,
        sample_pass_count=sample_pass_count,
        sample_fail_count=sample_fail_count,
        sample_fail_rate=fail_rate,
    )
