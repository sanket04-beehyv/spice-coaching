"""Stage 2b — cross-source fusion service.

Takes per-source candidates from Stage 2a and produces a post-fusion list
where candidate pairs covering the same CHW behavioural unit across
different source_document_ids are merged into a single fused candidate.

Design note — why this exists separate from Stage 2a:
The shared-context-in-chunker experiment (reverted) put both source corpora
in front of the LLM during identification. Result: 45 single-source
candidates collapsed to 8, all single-source. Mixing extraction (raw corpus
→ candidates) and alignment (which candidates pair?) in one LLM call put
the model in the worst position. Stage 2b separates the concerns: per-source
identification handles extraction unchanged; this service handles alignment
on a small, bounded metadata input (~3K tokens for 45 candidates).

Operates on candidate METADATA only (title + scope_summary +
source_document_id). No raw corpus content, no provenance lookup. The
fused candidate's source_provenance is the union of constituents'.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    TraceContext,
)

from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.llm_response_resolver import resolve_parsed_json
from platform_service.services.prompt_registry import CROSS_SOURCE_FUSER_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.cross_source_fuser_variables import (
    build_cross_source_fuser_variables,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FusionGroup:
    """One pairing emitted by the fuser. `constituent_ids` references
    candidate IDs from the input list; `merged_*` fields synthesise the
    fused module's title/scope/rationale."""

    constituent_ids: list[uuid.UUID]
    merged_title: str
    merged_scope_summary: str
    pairing_rationale: str


@dataclass(frozen=True)
class CrossSourceFuserResult:
    """Outcome of one fusion call.

    `fusion_groups` is the list of valid groups (each spanning ≥2 distinct
    source_document_ids). `unfused_ids` is the candidate IDs left as-is —
    i.e., NOT merged into any group. Together they partition the input
    candidate list (no candidate appears in both, every candidate appears
    in exactly one of them).
    """

    fusion_groups: list[FusionGroup]
    unfused_ids: list[uuid.UUID]
    raw_response_text: str


class CrossSourceFuserError(Exception):
    """Raised when ai-runtime errors out or the LLM output is unusable."""


class CrossSourceFuser:
    """Stage 2b: pair candidates across source_document_ids."""

    def __init__(
        self,
        client: AIRuntimeClient | None = None,
    ) -> None:
        self._client = client or get_ai_client()

    async def fuse(
        self,
        candidates: list[dict[str, Any]],
        *,
        trace_context: TraceContext | None = None,
    ) -> CrossSourceFuserResult:
        """Run the cross-source fusion call.

        `candidates` must be a list of dicts each carrying at minimum:
        - `id` (uuid)
        - `source_document_id` (uuid)
        - `proposed_title` (str)
        - `scope_summary` (str)

        Pre-conditions:
        - candidates contains ≥2 distinct `source_document_id` values.
          When all candidates are from one source there is no fusion to
          do; caller should bypass this service.

        Post-conditions:
        - Every fusion group spans ≥2 distinct source_document_ids.
        - Every candidate ID appears in EITHER a fusion group OR
          `unfused_ids`, never both, never neither.
        - Hallucinated IDs (LLM emitting candidate IDs not in the input)
          are dropped from groups; if a group falls below 2 valid IDs
          after the drop, the group is rejected and its valid IDs are
          treated as unfused.
        """
        if len(candidates) < 2:
            return CrossSourceFuserResult(
                fusion_groups=[],
                unfused_ids=[uuid.UUID(str(c["id"])) for c in candidates],
                raw_response_text="",
            )

        distinct_sources = {str(c.get("source_document_id")) for c in candidates}
        if len(distinct_sources) < 2:
            logger.info(
                "Stage 2b: only %d distinct source(s) in input — skipping fusion call",
                len(distinct_sources),
            )
            return CrossSourceFuserResult(
                fusion_groups=[],
                unfused_ids=[uuid.UUID(str(c["id"])) for c in candidates],
                raw_response_text="",
            )

        rendered = await PromptTemplateService().render(
            None,
            template_id=CROSS_SOURCE_FUSER_TEMPLATE_ID,
            variant_key=None,
            variables=build_cross_source_fuser_variables(candidates=candidates),
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.CROSS_SOURCE_FUSION,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(
                language="en",
                output_format="json",
            ),
            trace_context=trace_context or TraceContext(),
        )

        response = await self._client.generate(request)
        if response.error:
            raise CrossSourceFuserError(f"ai-runtime error: {response.error}")

        try:
            payload = resolve_parsed_json(response, fallback_text=response.raw_text or "{}")
        except json.JSONDecodeError as exc:
            raise CrossSourceFuserError(f"LLM output is not valid JSON: {exc}") from exc

        groups, unfused = _validate_and_partition(payload, candidates)
        logger.info(
            "Stage 2b cross-source fusion: %d input candidates from %d sources → "
            "%d fusion groups (%d candidates fused) + %d unfused",
            len(candidates),
            len(distinct_sources),
            len(groups),
            sum(len(g.constituent_ids) for g in groups),
            len(unfused),
        )
        for g in groups:
            logger.info(
                "Stage 2b fusion group: %d constituents → %r",
                len(g.constituent_ids),
                g.merged_title,
            )
        return CrossSourceFuserResult(
            fusion_groups=groups,
            unfused_ids=unfused,
            raw_response_text=response.raw_text,
        )


def _validate_and_partition(
    payload: Any,
    candidates: list[dict[str, Any]],
) -> tuple[list[FusionGroup], list[uuid.UUID]]:
    """Validate the LLM output and partition the input candidate set into
    (fusion_groups, unfused_ids).

    Validation rules:
    - Every constituent_id must be a real ID in the input candidate set
      (LLM hallucinations are dropped).
    - Every fusion group must contain ≥2 distinct source_document_ids
      after dropping hallucinations.
    - A candidate appears in AT MOST ONE fusion group; if the LLM places
      the same id in two groups, the first occurrence wins (deterministic).
    """
    candidate_by_id: dict[uuid.UUID, dict[str, Any]] = {}
    for c in candidates:
        try:
            cid = uuid.UUID(str(c["id"]))
        except (KeyError, TypeError, ValueError):
            continue
        candidate_by_id[cid] = c

    groups: list[FusionGroup] = []
    used_ids: set[uuid.UUID] = set()

    raw_groups: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        raw_groups = list(payload.get("fusion_groups") or [])

    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        raw_ids = raw.get("candidate_ids") or []
        if not isinstance(raw_ids, list):
            continue
        valid_ids: list[uuid.UUID] = []
        for raw_id in raw_ids:
            try:
                cid = uuid.UUID(str(raw_id))
            except (TypeError, ValueError):
                logger.warning("Stage 2b: dropping invalid candidate_id %r in fusion group", raw_id)
                continue
            if cid not in candidate_by_id:
                logger.warning("Stage 2b: dropping hallucinated candidate_id %s (not in input set)", cid)
                continue
            if cid in used_ids:
                logger.warning(
                    "Stage 2b: candidate %s appears in multiple groups; keeping first occurrence",
                    cid,
                )
                continue
            valid_ids.append(cid)
        if len(valid_ids) < 2:
            logger.warning("Stage 2b: rejecting group with %d valid constituents (need ≥2)", len(valid_ids))
            continue
        # Group must span ≥2 distinct source_document_ids
        distinct_sources = {str(candidate_by_id[cid].get("source_document_id")) for cid in valid_ids}
        if len(distinct_sources) < 2:
            logger.warning(
                "Stage 2b: rejecting group — all %d constituents from same source",
                len(valid_ids),
            )
            continue
        groups.append(
            FusionGroup(
                constituent_ids=valid_ids,
                merged_title=str(raw.get("merged_title") or "").strip()
                or candidate_by_id[valid_ids[0]].get("proposed_title", ""),
                merged_scope_summary=str(raw.get("merged_scope_summary") or "").strip()
                or candidate_by_id[valid_ids[0]].get("scope_summary", ""),
                pairing_rationale=str(raw.get("pairing_rationale") or "").strip(),
            )
        )
        used_ids.update(valid_ids)

    unfused = [cid for cid in candidate_by_id if cid not in used_ids]
    return groups, unfused
