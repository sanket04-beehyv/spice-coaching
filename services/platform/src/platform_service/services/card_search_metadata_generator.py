"""LLM generator: produce locale-keyed search metadata for module cards."""

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
from mc_foundation.locale import LOCALIZED_CARD_TEXT_FIELDS

from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.localized import (
    deployment_locales,
    localized_list_field_has_content,
    localized_synonyms_has_content,
    migrate_legacy_card,
    migrate_legacy_suffix_list_field,
)
from platform_service.services.card_body_text import card_body_plain_text
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.module_search_metadata_generator import (
    _clean_str_list,
    _localized_synonyms,
)
from platform_service.services.prompt_registry import CARD_SEARCH_METADATA_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.card_search_metadata_variables import (
    build_card_search_metadata_variables,
)

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CardSearchMetadataResult:
    metadata: dict[str, Any] | None
    error: str | None = None


@dataclass(frozen=True)
class CardSearchMetadataBatchResult:
    metadata_by_index: dict[int, dict[str, Any]]
    failed_indices: list[int]
    error: str | None = None


def _truncate(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def module_context_for_search_metadata(module: Module) -> dict[str, Any]:
    """Build module-level context shared across all cards in a batch prompt."""
    return {
        "title": module.title_localized,
        "description": module.description_localized,
        "module_domain": module.domain,
        "module_type": module.module_type,
    }


def _truncated_localized_card_fields(
    card: dict[str, Any],
    *,
    primary: str,
) -> dict[str, Any]:
    """Project locale-keyed card text fields with per-locale truncation."""
    migrated = migrate_legacy_card(dict(card), primary=primary)
    out: dict[str, Any] = {}
    body_limit = 600
    short_limit = 200
    for field in LOCALIZED_CARD_TEXT_FIELDS:
        value = migrated.get(field)
        if not isinstance(value, dict):
            continue
        raw = value.get(primary)
        if raw is None:
            continue
        limit = body_limit if field in ("title", "body") else short_limit
        text = card_body_plain_text(raw) if field == "body" else str(raw).strip()
        piece = _truncate(text, limit)
        if piece:
            out[field] = {primary: piece}
    return out


def card_payload_for_search_metadata(
    module: Module,
    card: dict[str, Any],
    *,
    card_index: int,
) -> dict[str, Any]:
    """Build a compact card summary for the search metadata prompt."""
    settings = get_settings()
    primary = deployment_locales(settings)
    return {
        **module_context_for_search_metadata(module),
        "card_index": card_index,
        **_truncated_localized_card_fields(card, primary=primary),
    }


def card_only_payload_for_search_metadata(
    card: dict[str, Any],
    *,
    card_index: int,
) -> dict[str, Any]:
    """Build a card summary without repeated module context (for batch prompts)."""
    settings = get_settings()
    primary = deployment_locales(settings)
    return {
        "card_index": card_index,
        **_truncated_localized_card_fields(card, primary=primary),
    }


def _localized_str_list(
    payload: dict[str, Any],
    field: str,
    *,
    primary: str,
    max_items: int,
) -> dict[str, list[str]]:
    data = dict(payload)
    migrate_legacy_suffix_list_field(data, field, primary=primary)
    value = data.get(field)
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    primary_list = _clean_str_list(value.get(primary), max_items=max_items)
    if primary_list:
        out[primary] = primary_list
    return out


def normalize_card_search_metadata(
    payload: dict[str, Any],
    *,
    max_retrieval_hints: int,
    max_keywords: int,
    max_synonyms: int,
    max_questions: int,
    primary_locale: str | None = None,
) -> dict[str, Any]:
    """Validate and cap LLM output to the persisted card search metadata schema."""
    settings = get_settings()
    primary = primary_locale or settings.deployment_primary_locale

    return {
        "schema_version": _SCHEMA_VERSION,
        "retrieval_hints": _localized_str_list(
            payload,
            "retrieval_hints",
            primary=primary,
            max_items=max_retrieval_hints,
        ),
        "keywords": _localized_str_list(payload, "keywords", primary=primary, max_items=max_keywords),
        "synonyms": _localized_synonyms(payload, "synonyms", primary=primary, max_items=max_synonyms),
        "questions": _localized_str_list(payload, "questions", primary=primary, max_items=max_questions),
    }


def card_metadata_has_searchable_content(metadata: dict[str, Any]) -> bool:
    """Return True when at least one lexical field is non-empty."""
    for key in ("retrieval_hints", "keywords", "questions"):
        if localized_list_field_has_content(metadata, key):
            return True
    return localized_synonyms_has_content(metadata)


def parse_batch_card_search_metadata(
    payload: dict[str, Any],
    *,
    requested_indices: set[int],
    max_retrieval_hints: int,
    max_keywords: int,
    max_synonyms: int,
    max_questions: int,
    primary_locale: str | None = None,
) -> tuple[dict[int, dict[str, Any]], list[int]]:
    """Parse and normalize per-card metadata from a batch LLM response."""
    metadata_by_index: dict[int, dict[str, Any]] = {}
    failed_indices: list[int] = []
    seen_indices: set[int] = set()

    cards_raw = payload.get("cards")
    if not isinstance(cards_raw, list):
        return {}, sorted(requested_indices)

    for entry in cards_raw:
        if not isinstance(entry, dict):
            continue
        card_index_raw = entry.get("card_index")
        if not isinstance(card_index_raw, int):
            continue
        if card_index_raw not in requested_indices:
            continue
        if card_index_raw in seen_indices:
            continue
        seen_indices.add(card_index_raw)

        metadata = normalize_card_search_metadata(
            entry,
            max_retrieval_hints=max_retrieval_hints,
            max_keywords=max_keywords,
            max_synonyms=max_synonyms,
            max_questions=max_questions,
            primary_locale=primary_locale,
        )
        if card_metadata_has_searchable_content(metadata):
            metadata_by_index[card_index_raw] = metadata
        else:
            failed_indices.append(card_index_raw)

    for card_index in requested_indices:
        if card_index not in seen_indices:
            failed_indices.append(card_index)

    failed_indices = sorted(set(failed_indices) - set(metadata_by_index))
    return metadata_by_index, failed_indices


class CardSearchMetadataGenerator:
    def __init__(
        self,
        *,
        client: AIRuntimeClient | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client or get_ai_client()
        self._max_retrieval_hints = settings.card_search_metadata_max_retrieval_hints
        self._max_keywords = settings.card_search_metadata_max_keywords
        self._max_synonyms = settings.card_search_metadata_max_synonyms
        self._max_questions = settings.card_search_metadata_max_questions

    async def generate_for_module(
        self,
        module: Module,
        card_indices: list[int],
        *,
        cards: list[dict[str, Any]] | None = None,
    ) -> CardSearchMetadataBatchResult:
        if not card_indices:
            return CardSearchMetadataBatchResult(metadata_by_index={}, failed_indices=[])

        card_list = cards if cards is not None else []
        card_payloads: list[dict[str, Any]] = []
        for card_index in card_indices:
            if card_index < 0 or card_index >= len(card_list):
                continue
            card = card_list[card_index]
            if not isinstance(card, dict):
                continue
            card_payloads.append(card_only_payload_for_search_metadata(card, card_index=card_index))

        if not card_payloads:
            return CardSearchMetadataBatchResult(
                metadata_by_index={},
                failed_indices=sorted(card_indices),
            )

        settings = get_settings()
        requested_indices = {payload["card_index"] for payload in card_payloads}

        rendered = await PromptTemplateService().render(
            None,
            template_id=CARD_SEARCH_METADATA_TEMPLATE_ID,
            variant_key=None,
            variables=build_card_search_metadata_variables(
                max_retrieval_hints=self._max_retrieval_hints,
                max_keywords=self._max_keywords,
                max_synonyms=self._max_synonyms,
                max_questions=self._max_questions,
                module_context=module_context_for_search_metadata(module),
                card_payloads=card_payloads,
                deployment_primary_locale=settings.deployment_primary_locale,
                deployment_region_context=settings.deployment_region_context,
            ),
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.CARD_SEARCH_METADATA,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(language="en", output_format="json"),
            trace_context=TraceContext(),
        )
        response = await self._client.generate(request)
        if response.error:
            logger.error(
                "Card search metadata generator: ai-runtime error module=%s: %s",
                module.id,
                response.error,
            )
            return CardSearchMetadataBatchResult(
                metadata_by_index={},
                failed_indices=sorted(requested_indices),
                error=str(response.error),
            )

        try:
            payload = resolve_parsed_dict(response)
        except json.JSONDecodeError as exc:
            logger.error(
                "Card search metadata generator: LLM output not JSON module=%s: %s",
                module.id,
                exc,
            )
            return CardSearchMetadataBatchResult(
                metadata_by_index={},
                failed_indices=sorted(requested_indices),
                error="invalid_json",
            )
        except TypeError:
            logger.error(
                "Card search metadata generator: unexpected payload shape module=%s",
                module.id,
            )
            return CardSearchMetadataBatchResult(
                metadata_by_index={},
                failed_indices=sorted(requested_indices),
                error="invalid_payload_shape",
            )

        metadata_by_index, failed_indices = parse_batch_card_search_metadata(
            payload,
            requested_indices=requested_indices,
            max_retrieval_hints=self._max_retrieval_hints,
            max_keywords=self._max_keywords,
            max_synonyms=self._max_synonyms,
            max_questions=self._max_questions,
            primary_locale=settings.deployment_primary_locale,
        )
        return CardSearchMetadataBatchResult(
            metadata_by_index=metadata_by_index,
            failed_indices=failed_indices,
        )

    async def generate(
        self,
        module: Module,
        card: dict[str, Any],
        *,
        card_index: int,
    ) -> CardSearchMetadataResult:
        padded_cards: list[dict[str, Any]] = [{} for _ in range(card_index)] + [card]
        batch_result = await self.generate_for_module(
            module,
            [card_index],
            cards=padded_cards,
        )
        metadata = batch_result.metadata_by_index.get(card_index)
        if metadata is not None:
            return CardSearchMetadataResult(metadata=metadata)
        error = batch_result.error or "empty_metadata"
        return CardSearchMetadataResult(metadata=None, error=error)
