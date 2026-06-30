"""LLM generator: produce bilingual search metadata for module retrieval."""

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
from platform_service.localized import localized_list_field_has_content, migrate_legacy_suffix_list_field
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.module_gap_classifier import module_payload_for_classification
from platform_service.services.prompts.search_metadata_prompt import (
    SEARCH_METADATA_TEMPLATE_ID,
    SEARCH_METADATA_TEMPLATE_VERSION,
    render_human_message,
    render_system_prompt,
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
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    primary_list = _clean_str_list(value.get(primary), max_items=max_items)
    if primary_list:
        out[primary] = primary_list
    return out


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
        "synonyms_en": _clean_synonyms(payload.get("synonyms_en"), max_items=max_synonyms),
        "topic_tags": _clean_str_list(payload.get("topic_tags"), max_items=max_tags),
        "clinical_conditions": _clean_str_list(payload.get("clinical_conditions"), max_items=max_tags),
        "audience": audience,
        "rationale": rationale,
    }


def metadata_has_searchable_content(metadata: dict[str, Any]) -> bool:
    """Return True when at least one lexical field is non-empty."""
    for key in ("keywords", "search_phrases"):
        if localized_list_field_has_content(metadata, key):
            return True
    for key in ("topic_tags", "clinical_conditions"):
        value = metadata.get(key)
        if isinstance(value, list) and value:
            return True
    return bool(metadata.get("synonyms_en"))


class ModuleSearchMetadataGenerator:
    def __init__(
        self,
        *,
        client: AIRuntimeClient | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client or get_ai_client()
        self._model = model or settings.text_model
        self._max_keywords = settings.search_metadata_max_keywords
        self._max_search_phrases = settings.search_metadata_max_search_phrases
        self._max_synonyms = settings.search_metadata_max_synonyms
        self._max_tags = settings.search_metadata_max_tags

    async def generate(self, module: Module) -> SearchMetadataResult:
        settings = get_settings()
        module_payload = module_payload_for_classification(module)

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.MODULE_SEARCH_METADATA,
            model_policy=ModelPolicy(model=self._model),
            prompt=PromptSpec(
                template_id=SEARCH_METADATA_TEMPLATE_ID,
                template_version=SEARCH_METADATA_TEMPLATE_VERSION,
                resolved_system_prompt=render_system_prompt(
                    max_keywords=self._max_keywords,
                    max_search_phrases=self._max_search_phrases,
                    deployment_primary_locale=settings.deployment_primary_locale,
                    deployment_region_context=settings.deployment_region_context,
                ),
                resolved_human_message=render_human_message(module_payload=module_payload),
            ),
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
