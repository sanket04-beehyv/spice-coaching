"""Stage 2-draft — merge newly drafted cards with a similar active module."""

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
    ModelPolicy,
    PromptSpec,
    TraceContext,
)

from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.card_normalisation import normalise_draft_card
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.prompts.published_module_merger_prompt import (
    PUBLISHED_MODULE_MERGER_TEMPLATE_ID,
    PUBLISHED_MODULE_MERGER_TEMPLATE_VERSION,
    render_human_message,
    render_system_prompt,
)
from platform_service.services.text_similarity import trigram_similarity

logger = logging.getLogger(__name__)

_CARD_TEXT_FIELDS = (
    "title_bn",
    "title_en",
    "body_bn",
    "body_en",
    "next_action_bn",
    "next_action_en",
    "previous_practice_bn",
    "previous_practice_en",
    "current_practice_bn",
    "current_practice_en",
    "rationale_for_change_bn",
    "rationale_for_change_en",
)


@dataclass(frozen=True)
class PublishedModuleMergerResult:
    """Outcome of one module-merge LLM call."""

    matched_module_id: uuid.UUID | None
    match_rationale: str | None
    merged_cards: list[dict[str, Any]]


class PublishedModuleMergerError(Exception):
    """Raised when ai-runtime errors out or the LLM output is unusable."""


class PublishedModuleMerger:
    """LLM: pick best active (non-retired) match (if any) and merge card sets."""

    def __init__(
        self,
        client: AIRuntimeClient | None = None,
        *,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client or get_ai_client()
        self._model = model or settings.text_model

    async def merge(
        self,
        *,
        candidate: dict[str, Any],
        new_cards: list[dict[str, Any]],
        valid_block_ids: set[uuid.UUID],
        trace_context: TraceContext | None = None,
        existing_modules: list[dict[str, Any]] | None = None,
        # Backwards-compat alias for tests/callers still passing this name.
        published_modules: list[dict[str, Any]] | None = None,
    ) -> PublishedModuleMergerResult:
        """Run merge when the active-module list is non-empty.

        Each entry must include `module_id`, titles, `lifecycle_status`, and
        `cards` (runtime card dicts). Caller pre-filters the list size.
        """
        modules = (
            existing_modules
            if existing_modules is not None
            else (published_modules if published_modules is not None else [])
        )
        if not modules:
            return PublishedModuleMergerResult(
                matched_module_id=None,
                match_rationale=None,
                merged_cards=list(new_cards),
            )

        settings = get_settings()
        prefiltered = _prefilter_existing(
            candidate.get("proposed_title", "") or "",
            modules,
            new_cards=new_cards,
            limit=settings.stage_d_published_merge_prefilter_limit,
        )

        system_prompt = render_system_prompt(
            card_min_count=settings.card_min_count,
            card_max_count=settings.card_max_count,
        )
        human_message = render_human_message(
            candidate=candidate,
            new_cards=new_cards,
            existing_modules=prefiltered,
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.MODULE_PUBLISHED_MERGE,
            model_policy=ModelPolicy(model=self._model),
            prompt=PromptSpec(
                template_id=PUBLISHED_MODULE_MERGER_TEMPLATE_ID,
                template_version=PUBLISHED_MODULE_MERGER_TEMPLATE_VERSION,
                resolved_system_prompt=system_prompt,
                resolved_human_message=human_message,
            ),
            constraints=GenerationConstraints(
                language="en",
                output_format="json",
                max_tokens=settings.stage_d_published_merge_max_output_tokens,
            ),
            trace_context=trace_context or TraceContext(),
        )
        response = await self._client.generate(request)
        if response.error:
            raise PublishedModuleMergerError(f"ai-runtime error: {response.error}")

        try:
            payload = resolve_parsed_dict(response, fallback_text=response.raw_text or "{}")
        except json.JSONDecodeError as exc:
            raise PublishedModuleMergerError(f"LLM output is not valid JSON: {exc}") from exc
        except TypeError as exc:
            raise PublishedModuleMergerError(str(exc)) from exc

        return _parse_merge_payload(
            payload,
            new_cards=new_cards,
            existing_modules=prefiltered,
            candidate=candidate,
            valid_block_ids=valid_block_ids,
        )


def _card_fingerprint(card: dict[str, Any]) -> str:
    """Normalised text blob for similarity scoring."""
    parts: list[str] = []
    for field in _CARD_TEXT_FIELDS:
        value = (card.get(field) or "").strip()
        if value:
            parts.append(value)
    return " ".join(parts)


def _card_block_ids(card: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for raw in card.get("source_block_ids") or []:
        if raw is not None and str(raw).strip():
            ids.add(str(raw).strip().lower())
    return ids


def _existing_card_matches_new(
    existing_card: dict[str, Any],
    new_cards: list[dict[str, Any]],
    *,
    card_threshold: float,
) -> bool:
    """True if existing card shares a block id or exceeds text similarity with some new card."""
    existing_blocks = _card_block_ids(existing_card)
    if existing_blocks:
        for new_card in new_cards:
            if existing_blocks & _card_block_ids(new_card):
                return True
    if not new_cards:
        return False
    return _best_card_similarity(existing_card, new_cards) >= card_threshold


def _best_card_similarity(existing_card: dict[str, Any], new_cards: list[dict[str, Any]]) -> float:
    fp = _card_fingerprint(existing_card)
    if not fp:
        return 0.0
    best = 0.0
    for new_card in new_cards:
        sim = trigram_similarity(fp, _card_fingerprint(new_card))
        if sim > best:
            best = sim
    return best


def _existing_card_match_ratio(
    existing_cards: list[dict[str, Any]],
    new_cards: list[dict[str, Any]],
    *,
    card_threshold: float,
) -> float:
    if not existing_cards:
        return 0.0
    matched = sum(
        1
        for card in existing_cards
        if _existing_card_matches_new(card, new_cards, card_threshold=card_threshold)
    )
    return matched / len(existing_cards)


def _module_content_similarity(
    existing_cards: list[dict[str, Any]],
    new_cards: list[dict[str, Any]],
) -> float:
    existing_fp = " ".join(_card_fingerprint(c) for c in existing_cards).strip()
    new_fp = " ".join(_card_fingerprint(c) for c in new_cards).strip()
    if not existing_fp or not new_fp:
        return 0.0
    return trigram_similarity(existing_fp, new_fp)


def _passes_merge_content_gate(
    existing_cards: list[dict[str, Any]],
    new_cards: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Deterministic dual gate: per-card majority + whole-module similarity."""
    settings = get_settings()
    card_ratio = _existing_card_match_ratio(
        existing_cards,
        new_cards,
        card_threshold=settings.stage_d_published_merge_card_similarity_threshold,
    )
    module_sim = _module_content_similarity(existing_cards, new_cards)
    min_ratio = settings.stage_d_published_merge_min_existing_card_match_ratio
    min_module_sim = settings.stage_d_published_merge_module_similarity_threshold

    matched_count = round(card_ratio * len(existing_cards)) if existing_cards else 0
    detail = (
        f"content_gate: {matched_count}/{len(existing_cards)} existing cards matched "
        f"(ratio={card_ratio:.2f}, need>={min_ratio:.2f}), "
        f"module_sim={module_sim:.2f} (need>={min_module_sim:.2f})"
    )

    ok = card_ratio >= min_ratio and module_sim >= min_module_sim
    return ok, detail


def _prefilter_existing(
    candidate_title: str,
    existing_modules: list[dict[str, Any]],
    *,
    new_cards: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Keep top-K existing modules by title or card-content similarity."""
    if len(existing_modules) <= limit:
        return existing_modules
    scored: list[tuple[float, dict[str, Any]]] = []
    for mod in existing_modules:
        title = (mod.get("title_en") or mod.get("title_bn") or "").strip()
        title_score = trigram_similarity(candidate_title, title)
        mod_cards = mod.get("cards") if isinstance(mod.get("cards"), list) else []
        content_score = _module_content_similarity(mod_cards, new_cards)
        score = max(title_score, content_score)
        scored.append((score, mod))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [mod for _, mod in scored[:limit]]


def _find_existing_module(
    existing_modules: list[dict[str, Any]],
    module_id: uuid.UUID,
) -> dict[str, Any] | None:
    key = str(module_id)
    for mod in existing_modules:
        if str(mod.get("module_id")) == key:
            return mod
    return None


def _parse_merge_payload(
    payload: dict[str, Any],
    *,
    new_cards: list[dict[str, Any]],
    existing_modules: list[dict[str, Any]],
    candidate: dict[str, Any],
    valid_block_ids: set[uuid.UUID],
) -> PublishedModuleMergerResult:
    settings = get_settings()
    module_type = candidate.get("proposed_module_type", "refresher")
    rationale = str(payload.get("match_rationale") or "").strip() or None

    allowed_ids = {str(m["module_id"]) for m in existing_modules if m.get("module_id")}
    raw_match = payload.get("matched_module_id")
    matched_id: uuid.UUID | None = None
    if raw_match is not None and str(raw_match).lower() not in ("null", "none", ""):
        try:
            mid = uuid.UUID(str(raw_match))
        except (TypeError, ValueError) as exc:
            raise PublishedModuleMergerError(f"invalid matched_module_id: {raw_match!r}") from exc
        if str(mid) not in allowed_ids:
            raise PublishedModuleMergerError(f"matched_module_id {mid} not in existing-module candidate set")
        matched_id = mid

    cards_raw = payload.get("merged_cards")
    if not isinstance(cards_raw, list):
        raise PublishedModuleMergerError("LLM output missing 'merged_cards' list")

    cards: list[dict[str, Any]] = []
    for raw_card in cards_raw:
        if not isinstance(raw_card, dict):
            continue
        normalised = normalise_draft_card(
            raw_card,
            module_type=module_type,
            valid_block_ids=valid_block_ids,
        )
        if normalised is not None:
            cards.append(normalised)

    if len(cards) > settings.card_max_count:
        logger.info(
            "Module merge returned %d cards; capping to %d",
            len(cards),
            settings.card_max_count,
        )
        cards = cards[: settings.card_max_count]

    if matched_id is None:
        return PublishedModuleMergerResult(
            matched_module_id=None,
            match_rationale=rationale,
            merged_cards=list(new_cards),
        )

    if not cards:
        raise PublishedModuleMergerError("matched merge produced no valid cards")

    matched_mod = _find_existing_module(existing_modules, matched_id)
    existing_cards = (
        matched_mod.get("cards") if matched_mod and isinstance(matched_mod.get("cards"), list) else []
    )
    gate_ok, gate_detail = _passes_merge_content_gate(existing_cards, new_cards)
    if not gate_ok:
        logger.info(
            "Module merge rejected by content gate for matched_module_id=%s: %s",
            matched_id,
            gate_detail,
        )
        rejected_rationale = rationale
        if rejected_rationale:
            rejected_rationale = f"{rejected_rationale} [{gate_detail}]"
        else:
            rejected_rationale = gate_detail
        return PublishedModuleMergerResult(
            matched_module_id=None,
            match_rationale=rejected_rationale,
            merged_cards=list(new_cards),
        )

    return PublishedModuleMergerResult(
        matched_module_id=matched_id,
        match_rationale=rationale,
        merged_cards=cards,
    )


def published_module_to_merge_dict(module: Module) -> dict[str, Any]:
    """Serialize a Module ORM row for the merger prompt."""
    cards = (module.module_json or {}).get("cards", [])
    return {
        "module_id": str(module.id),
        "lifecycle_status": module.lifecycle_status,
        "title_en": module.title_en,
        "title_bn": module.title_bn,
        "description_bn": module.description_bn,
        "module_type": module.module_type,
        "domain": module.domain,
        "cards": cards if isinstance(cards, list) else [],
    }


__all__ = [
    "PublishedModuleMerger",
    "PublishedModuleMergerError",
    "PublishedModuleMergerResult",
    "published_module_to_merge_dict",
]
