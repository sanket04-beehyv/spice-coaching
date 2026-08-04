"""Stage 2 — corpus-level module identification.

Per `docs/ARCHITECTURE_RESET.md`. Builds a single LLM call (via ai-runtime,
GenerationType.MODULE_IDENTIFICATION) with the full corpus + outline +
already-published modules. Returns parsed candidate dicts ready to be
filtered and persisted.

The architecture-reset removed gap-driven prompting from this stage: gaps
are runtime telemetry, not source-document structure. The pipeline proposes
topic-modules with no notion of which behavioural gap they address; the
reviewer maps modules to gaps manually via the admin dashboard.

For corpora exceeding stage_c_max_corpus_tokens, the orchestrator calls
corpus_partitioner instead — this module is the single-call path.
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

from platform_service.config import get_settings
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.localized import (
    deployment_locales,
    migrate_legacy_suffix_field,
    primary_text,
)
from platform_service.module_domains import catalog_domain_label
from platform_service.services._json_salvage import (
    extract_inner_array,
    salvage_truncated_array,
)
from platform_service.services.ingestion_cardinality import IngestionCardinality
from platform_service.services.prompt_id_codec import (
    PromptIdCodec,
    translate_provenance_tokens,
)
from platform_service.services.prompt_registry import MODULE_IDENTIFIER_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.module_identifier_variables import (
    build_module_identifier_variables,
)

logger = logging.getLogger(__name__)


# Required fields on every parsed candidate (validation gate). Note:
# `behavioural_gap_code` was a v3.3 field; the architecture reset dropped
# it because Stage 2 no longer reasons about gaps.
_REQUIRED_CANDIDATE_FIELDS = (
    "proposed_title",
    "scope_summary",
    "source_provenance",
    "estimated_card_count",
    "estimated_quiz_count",
    "proposed_module_type",
)
_VALID_MODULE_TYPES = ("refresher", "content_update", "digital_proficiency", "initial_training")


@dataclass(frozen=True)
class ModuleIdentifierResult:
    """Outcome of one Stage C identification call."""

    candidates: list[dict[str, Any]]
    raw_response_text: str
    # True when the LLM hit max_output_tokens mid-stream and we recovered
    # a partial candidate list via salvage. The caller should record this
    # on the run/step output so the dashboard can surface "increase token
    # budget or partition more aggressively".
    truncated: bool = False


class ModuleIdentifierError(Exception):
    """Raised when ai-runtime errors out or the LLM output is unusable."""


class ModuleIdentifier:
    """Single-call Stage C corpus-level module identification."""

    def __init__(
        self,
        client: AIRuntimeClient | None = None,
    ) -> None:
        self._client = client or get_ai_client()

    async def identify(
        self,
        *,
        content_domains: set[str],
        already_published_modules: list[dict[str, Any]],
        document_outlines: list[dict[str, Any]],
        page_corpus: list[dict[str, Any]],
        valid_block_ids: set[uuid.UUID],
        valid_page_ids: set[uuid.UUID],
        ingestion_instructions: str | None = None,
        cardinality: IngestionCardinality | None = None,
        trace_context: TraceContext | None = None,
    ) -> ModuleIdentifierResult:
        """Run the Stage 2 identification call.

        Both `valid_block_ids` and `valid_page_ids` are required: the
        validator uses them to reject LLM-hallucinated UUIDs (the SK-PDF
        smoke run had Gemini copy-paste the same UUID across both slots,
        and across many entries — the page-id membership check is the
        broader defence and must not be opt-in).
        """
        settings = get_settings()
        card_lo, card_hi = (
            cardinality.card_bounds(settings)
            if cardinality is not None
            else (settings.card_min_count, settings.card_max_count)
        )
        quiz_lo, quiz_hi = (
            cardinality.quiz_bounds(settings)
            if cardinality is not None
            else (settings.quiz_min_questions, settings.quiz_max_questions)
        )
        codec = PromptIdCodec.from_corpus(page_corpus)
        logger.info(
            "Stage 2 identification codec built: docs=%d pages=%d blocks=%d",
            codec.doc_count,
            codec.page_count,
            codec.block_count,
        )
        rendered = await PromptTemplateService().render(
            None,
            template_id=MODULE_IDENTIFIER_TEMPLATE_ID,
            variant_key=None,
            variables=build_module_identifier_variables(
                already_published_modules=already_published_modules,
                document_outlines=document_outlines,
                page_corpus=page_corpus,
                codec=codec,
                content_domains=content_domains,
                card_min_count=card_lo,
                card_max_count=card_hi,
                quiz_min_count=quiz_lo,
                quiz_max_count=quiz_hi,
                target_cards=cardinality.target_cards if cardinality is not None else None,
                target_quizzes=cardinality.target_quizzes if cardinality is not None else None,
                deployment_primary_locale=settings.deployment_primary_locale,
                deployment_region_context=settings.deployment_region_context,
                ingestion_instructions=ingestion_instructions,
            ),
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.MODULE_IDENTIFICATION,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(
                language="en",
                output_format="json",
            ),
            trace_context=trace_context or TraceContext(),
        )

        response = await self._client.generate(request)

        # Parse output — accept either parsed_json or raw_text. Real
        # corpora regularly hit max_tokens mid-string; mirror the salvage
        # logic from ai-runtime's response_parser so a truncated response
        # still surfaces the candidates that DID complete instead of
        # propagating a JSONDecodeError up the orchestrator.
        payload: Any = response.parsed_json
        truncated = False
        if payload is None and response.raw_text.strip():
            try:
                payload = json.loads(response.raw_text, strict=False)
            except json.JSONDecodeError:
                # Wrapper-aware salvage. The identifier prompt instructs the
                # LLM to emit `{"candidates": [...]}` (object-wrapped). On
                # truncation, the wrapper survives but an inner element
                # didn't — try to recover the array. Fall back to bare-array
                # salvage for older LLMs that strip the wrapper despite the
                # schema.
                inner = extract_inner_array(response.raw_text, key="candidates")
                if inner is not None:
                    salvaged = salvage_truncated_array(inner)
                    if salvaged is not None:
                        payload = {"candidates": salvaged}
                        truncated = True
                if payload is None:
                    salvaged = salvage_truncated_array(response.raw_text)
                    if salvaged is not None:
                        payload = salvaged
                        truncated = True
                if payload is None:
                    raise ModuleIdentifierError(
                        "LLM output truncated mid-stream and no complete "
                        "candidate could be salvaged. Lower "
                        "stage_c_max_corpus_tokens to force partitioning, "
                        "or raise the module_identification max_tokens "
                        "profile in ai-runtime."
                    )
                recovered = payload.get("candidates", []) if isinstance(payload, dict) else payload
                logger.warning(
                    "Stage 2 LLM output was truncated; salvaged %d complete candidate(s)",
                    len(recovered) if isinstance(recovered, list) else 0,
                )
        if payload is None and response.error:
            raise ModuleIdentifierError(f"ai-runtime error: {response.error}")

        candidates = _extract_candidates(payload)
        # Translate short tokens (d1/p47/b231) back to real UUIDs before
        # validation. Best-effort: tokens that don't decode are left as
        # their original strings, and the existing UUID-format /
        # `valid_*_ids` membership checks reject them downstream — same
        # gate as before, just with an extra translation step in front.
        total_decoded = 0
        for cand in candidates:
            total_decoded += translate_provenance_tokens(cand, codec)
        if candidates:
            logger.info(
                "Stage 2 identification: token-translated %d provenance refs across %d candidates",
                total_decoded,
                len(candidates),
            )

        validated = [
            c
            for c in candidates
            if _validate_candidate(
                c,
                valid_block_ids=valid_block_ids,
                valid_page_ids=valid_page_ids,
                cardinality=cardinality,
            )
        ]
        validated = _apply_ingestion_instruction_gate(
            validated,
            ingestion_instructions=ingestion_instructions,
        )
        logger.info(
            "Stage 2 identification produced %d/%d valid candidates (rejected=%d, truncated=%s)",
            len(validated),
            len(candidates),
            len(candidates) - len(validated),
            truncated,
        )
        return ModuleIdentifierResult(
            candidates=validated,
            raw_response_text=response.raw_text,
            truncated=truncated,
        )


# Backwards-compat re-export so existing tests / external callers that
# imported `_salvage_truncated_array` from this module keep working.
_salvage_truncated_array = salvage_truncated_array


def _extract_candidates(payload: Any) -> list[dict[str, Any]]:
    """Pull the candidate list from the parsed payload.

    Tolerates either {"candidates": [...]} or a top-level array of candidates.
    """
    if isinstance(payload, dict):
        cands = payload.get("candidates")
        if isinstance(cands, list):
            return [c for c in cands if isinstance(c, dict)]
    if isinstance(payload, list):
        return [c for c in payload if isinstance(c, dict)]
    raise ModuleIdentifierError(
        f"LLM output shape not recognised; expected dict with 'candidates' or "
        f"top-level array, got {type(payload).__name__}"
    )


def _normalize_ingestion_instruction_rationale(cand: dict[str, Any]) -> None:
    """Strip and normalize optional ingestion_instruction_rationale in-place."""
    v = cand.get("ingestion_instruction_rationale")
    if v is None:
        cand["ingestion_instruction_rationale"] = None
        return
    if isinstance(v, str):
        cleaned = v.strip()
        cand["ingestion_instruction_rationale"] = cleaned or None
    else:
        cand["ingestion_instruction_rationale"] = None


def _apply_ingestion_instruction_gate(
    candidates: list[dict[str, Any]],
    *,
    ingestion_instructions: str | None,
) -> list[dict[str, Any]]:
    """When ingestion instructions are present, keep only candidates with rationale."""
    if not ingestion_instructions:
        for cand in candidates:
            _normalize_ingestion_instruction_rationale(cand)
        return candidates

    kept: list[dict[str, Any]] = []
    for cand in candidates:
        _normalize_ingestion_instruction_rationale(cand)
        if cand.get("ingestion_instruction_rationale"):
            kept.append(cand)
        else:
            logger.warning(
                "Rejecting candidate: missing ingestion_instruction_rationale "
                "while ingestion instructions are present (title=%r)",
                cand.get("proposed_title", ""),
            )
    return kept


def _normalize_candidate_domain(cand: dict[str, Any]) -> None:
    """Normalize optional Stage C ``domain`` onto the candidate dict in-place."""
    raw = cand.get("domain") or cand.get("proposed_domain")
    normalized = catalog_domain_label(str(raw) if raw is not None else None)
    if normalized is not None:
        cand["domain"] = normalized
        return
    cand.pop("domain", None)
    cand.pop("proposed_domain", None)


def _validate_candidate(
    cand: dict[str, Any],
    *,
    valid_block_ids: set[uuid.UUID],
    valid_page_ids: set[uuid.UUID] | None = None,
    cardinality: IngestionCardinality | None = None,
) -> bool:
    """Apply per-candidate validation. Returns True if the candidate survives.

    Rejects:
    - Missing required fields
    - module_type not in valid set
    - card/quiz counts out of bounds
    - Source-provenance citing block IDs not present in the corpus
    - Source-provenance with `source_page_id` equal to any cited
      `content_block_id` (different tables — same UUID is impossible;
      Gemini was observed to copy-paste the same UUID across both
      slots in long degenerate outputs).
    - Source-provenance with `source_page_id` not present in the corpus
      (when `valid_page_ids` is supplied — strongly recommended).
    """
    settings = get_settings()

    # Required fields
    for field in _REQUIRED_CANDIDATE_FIELDS:
        if field not in cand:
            logger.warning("Rejecting candidate: missing required field %r", field)
            return False

    # Optional localized descriptions (Stage C emits them; keep non-gating for
    # backwards compatibility with older cached/truncated outputs).
    description = cand.get("description")
    if isinstance(description, dict):
        primary = settings.deployment_primary_locale
        primary_val = description.get(primary)
        if isinstance(primary_val, str):
            description[primary] = primary_val.strip() or None
        cand["description"] = description
    else:
        primary = deployment_locales(settings)
        migrated = dict(cand)
        migrate_legacy_suffix_field(migrated, "description", primary=primary)
        desc = migrated.get("description")
        if isinstance(desc, dict):
            primary_val = primary_text(desc, settings=settings)
            if isinstance(primary_val, str) and primary_val.strip():
                cand["description"] = {primary: primary_val.strip()}

    _normalize_candidate_domain(cand)

    # Module type
    if cand["proposed_module_type"] not in _VALID_MODULE_TYPES:
        logger.warning("Rejecting candidate: invalid proposed_module_type %r", cand["proposed_module_type"])
        return False

    # Card / quiz counts
    try:
        card_n = int(cand["estimated_card_count"])
        quiz_n = int(cand["estimated_quiz_count"])
    except (TypeError, ValueError):
        logger.warning("Rejecting candidate: card/quiz counts not int")
        return False
    card_min, card_max = (
        cardinality.card_bounds(settings)
        if cardinality is not None
        else (settings.card_min_count, settings.card_max_count)
    )
    quiz_min, quiz_max = (
        cardinality.quiz_bounds(settings)
        if cardinality is not None
        else (settings.quiz_min_questions, settings.quiz_max_questions)
    )
    if not (card_min <= card_n <= card_max):
        logger.warning(
            "Rejecting candidate: estimated_card_count %d out of bounds [%d,%d]",
            card_n,
            card_min,
            card_max,
        )
        return False
    if not (quiz_min <= quiz_n <= quiz_max):
        logger.warning(
            "Rejecting candidate: estimated_quiz_count %d out of bounds [%d,%d]",
            quiz_n,
            quiz_min,
            quiz_max,
        )
        return False

    # Provenance: every cited block_id must exist in the corpus.
    provenance = cand.get("source_provenance", [])
    if not isinstance(provenance, list) or not provenance:
        logger.warning("Rejecting candidate: source_provenance empty/invalid")
        return False
    cited_blocks: list[uuid.UUID] = []
    for entry in provenance:
        if not isinstance(entry, dict):
            return False
        block_ids_raw = entry.get("content_block_ids", [])
        if not isinstance(block_ids_raw, list):
            return False
        # Per-entry source_page_id: must be a real page UUID, and must
        # NOT collide with any of the entry's content_block_ids (the
        # Gemini-copy-paste degenerate-output failure mode).
        page_id_raw = entry.get("source_page_id")
        try:
            page_id = uuid.UUID(str(page_id_raw)) if page_id_raw else None
        except ValueError:
            logger.warning("Rejecting candidate: invalid source_page_id %r", page_id_raw)
            return False
        entry_blocks: list[uuid.UUID] = []
        for raw in block_ids_raw:
            try:
                entry_blocks.append(uuid.UUID(str(raw)))
            except ValueError:
                logger.warning("Rejecting candidate: invalid block uuid %r", raw)
                return False
        if page_id is not None and page_id in entry_blocks:
            logger.warning(
                "Rejecting candidate: source_page_id %s collides with a "
                "content_block_id in the same provenance entry (LLM "
                "copy-paste — different tables, same UUID is impossible)",
                page_id,
            )
            return False
        if valid_page_ids is not None and page_id is not None and page_id not in valid_page_ids:
            logger.warning("Rejecting candidate: source_page_id %s not in corpus", page_id)
            return False
        cited_blocks.extend(entry_blocks)
    bad_blocks = [b for b in cited_blocks if b not in valid_block_ids]
    if bad_blocks:
        logger.warning("Rejecting candidate: %d cited block IDs not present in corpus", len(bad_blocks))
        return False

    return True
