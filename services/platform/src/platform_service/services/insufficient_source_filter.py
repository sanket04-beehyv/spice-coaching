"""Stage 2 insufficient-source heuristic — advisory, not gating.

Per `docs/ARCHITECTURE_RESET.md`. The function still computes an
`accepted` boolean and a list of `fail_reasons` — but Stage 2's caller
no longer rejects candidates based on `accepted`. Instead, the caller
records `fail_reasons` on `module_candidate_draft.quality_flags_jsonb`
and `module.quality_flags_jsonb` for the dashboard's "needs attention"
filter. This is the P1 fix from the audit: pipeline never silently drops
content; humans decide.

Heuristic:
- Total content_text across cited content_blocks ≥
  `stage_c_insufficient_source_min_tokens` → no `insufficient_tokens` flag.
- Cited blocks span ≥ `stage_c_insufficient_source_min_headings` distinct
  outline headings → no `insufficient_heading_coverage` flag (with a
  shallow-outline relaxation, see body).

The `accepted` field is preserved on `FilterDecision` for backwards
compatibility with tests/callers that still want a single yes/no.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from platform_service.config import get_settings


@dataclass(frozen=True)
class FilterDecision:
    """Outcome of evaluating one candidate against the precondition."""

    accepted: bool
    total_tokens: int
    distinct_headings: int
    fail_reasons: tuple[str, ...]


def _flatten_provenance(
    source_provenance: list[dict[str, Any]],
) -> list[tuple[UUID, UUID, list[UUID]]]:
    """Convert raw provenance jsonb to typed tuples (skipping malformed rows)."""
    flat: list[tuple[UUID, UUID, list[UUID]]] = []
    for entry in source_provenance:
        if not isinstance(entry, dict):
            continue
        try:
            doc_id = UUID(str(entry["source_document_id"]))
            page_id = UUID(str(entry["source_page_id"]))
            block_ids_raw = entry.get("content_block_ids", [])
            block_ids = [UUID(str(b)) for b in block_ids_raw if b]
        except (KeyError, ValueError):
            continue
        flat.append((doc_id, page_id, block_ids))
    return flat


def _outline_section_count(outline_jsonb: dict[str, Any] | None) -> int:
    """Count top-level + nested sections in the outline tree."""
    if not outline_jsonb:
        return 0
    sections = outline_jsonb.get("sections") if isinstance(outline_jsonb, dict) else None
    if not isinstance(sections, list):
        return 0

    def _count(nodes: list[Any]) -> int:
        total = 0
        for n in nodes:
            if not isinstance(n, dict):
                continue
            total += 1
            subs = n.get("subsections")
            if isinstance(subs, list):
                total += _count(subs)
        return total

    return _count(sections)


def evaluate_candidate(
    *,
    source_provenance: list[dict[str, Any]],
    cited_block_token_counts: dict[UUID, int],
    cited_block_headings: dict[UUID, list[str]],
    outline_section_count: int = 0,
) -> FilterDecision:
    """Apply the Stage C insufficient-source precondition.

    Inputs:
    - `source_provenance`: candidate's raw provenance (as it appears in
      module_candidate_draft.source_provenance_jsonb).
    - `cited_block_token_counts`: map of content_block_id → token count
      (from estimate_token_count or upstream measurement).
    - `cited_block_headings`: map of content_block_id → heading_path list
      from content_block.heading_path_jsonb.
    - `outline_section_count`: total sections in source_document.outline_jsonb;
      used as the "shallow outline" fallback when distinct headings count
      is too low.
    """
    settings = get_settings()
    fail_reasons: list[str] = []

    flat = _flatten_provenance(source_provenance)
    if not flat:
        return FilterDecision(
            accepted=False,
            total_tokens=0,
            distinct_headings=0,
            fail_reasons=("no_provenance",),
        )

    # Total tokens across all cited blocks (deduplicated).
    seen_blocks: set[UUID] = set()
    for _, _, block_ids in flat:
        seen_blocks.update(block_ids)
    total_tokens = sum(cited_block_token_counts.get(bid, 0) for bid in seen_blocks)

    # Distinct heading paths covered by cited blocks. Each heading_path is a
    # list — we hash by joined string for set semantics.
    distinct_paths: set[str] = set()
    for bid in seen_blocks:
        path = cited_block_headings.get(bid)
        if path:
            distinct_paths.add(" > ".join(path))
    distinct_headings = len(distinct_paths)

    # Token threshold check.
    if total_tokens < settings.stage_c_insufficient_source_min_tokens:
        fail_reasons.append("insufficient_tokens")

    # Heading-coverage check, with shallow-outline fallback to section count.
    min_headings = settings.stage_c_insufficient_source_min_headings
    if distinct_headings < min_headings:
        if outline_section_count == 0:
            # No outline at all (e.g. vision-extracted scan with no detectable
            # headings). Trust the token threshold alone — distinct_headings
            # would always be 0 here regardless of candidate quality.
            pass
        elif outline_section_count < min_headings:
            # Shallow but non-empty outline: accept on ≥ 2 distinct outline
            # references, otherwise flag.
            if distinct_headings < 2:
                fail_reasons.append("insufficient_section_coverage")
        else:
            fail_reasons.append("insufficient_heading_coverage")

    return FilterDecision(
        accepted=len(fail_reasons) == 0,
        total_tokens=total_tokens,
        distinct_headings=distinct_headings,
        fail_reasons=tuple(fail_reasons),
    )
