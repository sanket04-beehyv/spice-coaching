"""LLM generator: produce locale-keyed search metadata for module retrieval."""

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
from platform_service.db.models.module import Module
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.localized import (
    localized_list_field_has_content,
    localized_synonyms_has_content,
    migrate_legacy_suffix_list_field,
)
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.module_gap_classifier import module_payload_for_classification
from platform_service.services.prompt_registry import SEARCH_METADATA_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.search_metadata_variables import (
    build_search_metadata_variables,
)

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_DEFAULT_AUDIENCE = "chw_field_worker"


@dataclass(frozen=True)
class SearchMetadataResult:
    metadata: dict[str, Any] | None
    error: str | None = None


def _clean_str_list(raw: Any, *, max_items: int) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in out:
            continue
        out.append(text)
        if len(out) >= max_items:
            break
    return out


def _clean_synonyms(raw: Any, *, max_items: int) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        abbrev = key.strip()
        expanded = value.strip()
        if not abbrev or not expanded or abbrev in out:
            continue
        out[abbrev] = expanded
        if len(out) >= max_items:
            break
    return out


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
    if isinstance(value, list):
        primary_list = _clean_str_list(value, max_items=max_items)
        return {primary: primary_list} if primary_list else {}
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    primary_list = _clean_str_list(value.get(primary), max_items=max_items)
    if primary_list:
        out[primary] = primary_list
    return out


def _localized_synonyms(
    payload: dict[str, Any],
    field: str,
    *,
    primary: str,
    max_items: int,
) -> dict[str, dict[str, str]]:
    """Normalize synonyms to a primary-locale abbrev map, accepting legacy synonyms_en."""
    data = dict(payload)
    if "synonyms_en" in data and field not in data:
        cleaned = _clean_synonyms(data["synonyms_en"], max_items=max_items)
        return {primary: cleaned} if cleaned else {}
    value = data.get(field)
    if isinstance(value, dict):
        if value and all(isinstance(v, str) for v in value.values()):
            cleaned = _clean_synonyms(value, max_items=max_items)
            return {primary: cleaned} if cleaned else {}
        primary_map = value.get(primary)
        if isinstance(primary_map, dict):
            cleaned = _clean_synonyms(primary_map, max_items=max_items)
            return {primary: cleaned} if cleaned else {}
    return {}


def normalize_search_metadata(
    payload: dict[str, Any],
    *,
    max_keywords: int,
    max_search_phrases: int,
    max_synonyms: int,
    max_tags: int,
    primary_locale: str | None = None,
) -> dict[str, Any]:
    """Validate and cap LLM output to the persisted search metadata schema."""
    settings = get_settings()
    primary = primary_locale or settings.deployment_primary_locale

    rationale = str(payload.get("rationale") or "").strip()
    audience_raw = str(payload.get("audience") or _DEFAULT_AUDIENCE).strip()
    audience = audience_raw or _DEFAULT_AUDIENCE

    return {
        "schema_version": _SCHEMA_VERSION,
        "keywords": _localized_str_list(payload, "keywords", primary=primary, max_items=max_keywords),
        "search_phrases": _localized_str_list(
            payload,
            "search_phrases",
            primary=primary,
            max_items=max_search_phrases,
        ),
        "synonyms": _localized_synonyms(payload, "synonyms", primary=primary, max_items=max_synonyms),
        "topic_tags": _localized_str_list(payload, "topic_tags", primary=primary, max_items=max_tags),
        "clinical_conditions": _localized_str_list(
            payload,
            "clinical_conditions",
            primary=primary,
            max_items=max_tags,
        ),
        "audience": audience,
        "rationale": rationale,
    }


def metadata_has_searchable_content(metadata: dict[str, Any]) -> bool:
    """Return True when at least one lexical field is non-empty."""
    for key in ("keywords", "search_phrases", "topic_tags", "clinical_conditions"):
        if localized_list_field_has_content(metadata, key):
            return True
    return localized_synonyms_has_content(metadata)


class ModuleSearchMetadataGenerator:
    def __init__(
        self,
        *,
        client: AIRuntimeClient | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client or get_ai_client()
        self._max_keywords = settings.search_metadata_max_keywords
        self._max_search_phrases = settings.search_metadata_max_search_phrases
        self._max_synonyms = settings.search_metadata_max_synonyms
        self._max_tags = settings.search_metadata_max_tags

    async def generate(
        self,
        module: Module,
        *,
        cards: list[dict[str, Any]] | None = None,
    ) -> SearchMetadataResult:
        settings = get_settings()
        module_payload = module_payload_for_classification(module, cards=cards)

        rendered = await PromptTemplateService().render(
            None,
            template_id=SEARCH_METADATA_TEMPLATE_ID,
            variant_key=None,
            variables=build_search_metadata_variables(
                max_keywords=self._max_keywords,
                max_search_phrases=self._max_search_phrases,
                max_synonyms=self._max_synonyms,
                max_tags=self._max_tags,
                module_payload=module_payload,
                deployment_primary_locale=settings.deployment_primary_locale,
                deployment_region_context=settings.deployment_region_context,
            ),
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.MODULE_SEARCH_METADATA,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(language="en", output_format="json"),
            trace_context=TraceContext(),
        )
        response = await self._client.generate(request)
        if response.error:
            logger.error(
                "Search metadata generator: ai-runtime error for module %s: %s",
                module.id,
                response.error,
            )
            return SearchMetadataResult(metadata=None, error=str(response.error))

        try:
            payload = resolve_parsed_dict(response)
        except json.JSONDecodeError as exc:
            logger.error(
                "Search metadata generator: LLM output not JSON for module %s: %s",
                module.id,
                exc,
            )
            return SearchMetadataResult(metadata=None, error="invalid_json")
        except TypeError:
            logger.error(
                "Search metadata generator: unexpected payload shape for module %s",
                module.id,
            )
            return SearchMetadataResult(metadata=None, error="invalid_payload_shape")

        metadata = normalize_search_metadata(
            payload,
            max_keywords=self._max_keywords,
            max_search_phrases=self._max_search_phrases,
            max_synonyms=self._max_synonyms,
            max_tags=self._max_tags,
            primary_locale=settings.deployment_primary_locale,
        )
        if not metadata_has_searchable_content(metadata):
            return SearchMetadataResult(metadata=None, error="empty_metadata")

        return SearchMetadataResult(metadata=metadata)
