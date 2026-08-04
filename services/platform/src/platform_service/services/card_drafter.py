"""W-5 — Stage D card drafter via ai-runtime.

Per Pipeline v3.3 §7. Calls ai-runtime with GenerationType.CARD_DRAFTING and
parses the response into draft card dicts ready for snippet resolution and
persistence.
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
from platform_service.services.card_normalisation import normalise_draft_card
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.prompt_registry import CARD_DRAFTER_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.card_drafter_variables import build_card_drafter_variables

logger = logging.getLogger(__name__)


# Refusal reason vocabulary (per Pipeline v3.3 §7).
INSUFFICIENT_REASONS = (
    "no_actionable_content",
    "single_concept_only",
    "no_quiz_anchors",
)


@dataclass(frozen=True)
class CardDrafterResult:
    """Outcome of one card-drafting call."""

    cards: list[dict[str, Any]]
    insufficient_reason: str | None  # set when LLM refused; cards is empty


class CardDrafterError(Exception):
    """Raised when ai-runtime errors out or output is structurally bad."""


class CardDrafter:
    """Single-call card drafter."""

    def __init__(
        self,
        client: AIRuntimeClient | None = None,
    ) -> None:
        self._client = client or get_ai_client()

    async def draft(
        self,
        *,
        candidate: dict[str, Any],
        cited_blocks: list[dict[str, Any]],
        valid_block_ids: set[uuid.UUID],
        trace_context: TraceContext | None = None,
        card_min_count: int | None = None,
        card_max_count: int | None = None,
    ) -> CardDrafterResult:
        """Run the card drafter for one candidate.

        `cited_blocks` is the list of content_blocks the candidate's
        source_provenance pointed at (each a dict with content_block_id,
        block_type, content_text, content_language).
        """
        settings = get_settings()
        module_type = candidate.get("proposed_module_type", "refresher")
        resolved_card_min = card_min_count if card_min_count is not None else settings.card_min_count
        resolved_card_max = card_max_count if card_max_count is not None else settings.card_max_count

        rendered = await PromptTemplateService().render(
            None,
            template_id=CARD_DRAFTER_TEMPLATE_ID,
            variant_key=None,
            variables=build_card_drafter_variables(
                module_type=module_type,
                card_min_count=resolved_card_min,
                card_max_count=resolved_card_max,
                candidate=candidate,
                cited_blocks=cited_blocks,
                deployment_primary_locale=settings.deployment_primary_locale,
                deployment_region_context=settings.deployment_region_context,
            ),
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.CARD_DRAFTING,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(
                language=settings.deployment_primary_locale,
                output_format="json",
            ),
            trace_context=trace_context or TraceContext(),
        )
        response = await self._client.generate(request)
        if response.error:
            raise CardDrafterError(f"ai-runtime error: {response.error}")

        try:
            payload = resolve_parsed_dict(response)
        except json.JSONDecodeError as exc:
            raise CardDrafterError(f"LLM output is not valid JSON: {exc}") from exc
        except TypeError as exc:
            raise CardDrafterError(str(exc)) from exc

        # Refusal path
        reason = payload.get("insufficient_for_drafting")
        if reason:
            if reason not in INSUFFICIENT_REASONS:
                logger.warning(
                    "Drafter returned unknown insufficient_for_drafting reason %r — allowing through",
                    reason,
                )
            return CardDrafterResult(cards=[], insufficient_reason=str(reason))

        cards_raw = payload.get("cards")
        if not isinstance(cards_raw, list):
            raise CardDrafterError("LLM output missing 'cards' list")

        # Normalize + validate each card.
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

        # Cap at max — preserves the count guidance for the LLM but lets
        # us truncate long outputs gracefully.
        if len(cards) > resolved_card_max:
            logger.info("Card drafter returned %d cards; capping to %d", len(cards), resolved_card_max)
            cards = cards[:resolved_card_max]

        # Per the architecture reset, the drafter no longer rejects on
        # min-count: a 1-card module is still a renderable module, and
        # rejection wastes the candidate. The dashboard surfaces low-card
        # modules via `quality_flags` for clinician review instead.
        # If `cards == []` after validation, that's the "no salvageable
        # output" path — keep the explicit signal.
        if not cards:
            return CardDrafterResult(cards=[], insufficient_reason="no_actionable_content")

        # Always stamp a fresh server-issued UUID for `card_family_id`.
        # We never trust an LLM-supplied value (it's free-form and the
        # validator only requires a `str`) — runtime joins on this field
        # so a hallucinated non-UUID would corrupt module_quiz_question's
        # primary_card_family_id pointer.
        for c in cards:
            c["card_family_id"] = str(uuid.uuid4())

        return CardDrafterResult(cards=cards, insufficient_reason=None)
