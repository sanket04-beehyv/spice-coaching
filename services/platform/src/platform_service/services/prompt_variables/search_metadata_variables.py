"""Variable builders for search metadata prompt."""

from __future__ import annotations

import json
from typing import Any

from platform_service.config import get_settings
from platform_service.services.prompts.symbol_verbalization import (
    render_locale_list_map_field_schema,
    render_locale_synonym_map_field_schema,
)


def build_search_metadata_variables(
    *,
    max_keywords: int,
    max_search_phrases: int,
    max_synonyms: int,
    max_tags: int,
    module_payload: dict[str, Any],
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
) -> dict[str, str]:
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    region_context = deployment_region_context or settings.deployment_region_context
    return {
        "deployment_region_context": region_context,
        "max_keywords": str(max_keywords),
        "max_search_phrases": str(max_search_phrases),
        "max_synonyms": str(max_synonyms),
        "max_tags": str(max_tags),
        "keywords_field_schema": render_locale_list_map_field_schema(
            "keywords",
            primary_locale=primary_locale,
            max_items=max_keywords,
            description="short terms and clinical vocabulary",
        ),
        "search_phrases_field_schema": render_locale_list_map_field_schema(
            "search_phrases",
            primary_locale=primary_locale,
            max_items=max_search_phrases,
            description="natural-language questions or scenarios",
        ),
        "synonyms_field_schema": render_locale_synonym_map_field_schema(
            "synonyms",
            primary_locale=primary_locale,
            max_items=max_synonyms,
            description="abbreviation expansions in primary language",
        ),
        "topic_tags_field_schema": render_locale_list_map_field_schema(
            "topic_tags",
            primary_locale=primary_locale,
            max_items=max_tags,
            description="broad topical labels in snake_case",
        ),
        "clinical_conditions_field_schema": render_locale_list_map_field_schema(
            "clinical_conditions",
            primary_locale=primary_locale,
            max_items=max_tags,
            description="conditions or syndromes addressed",
        ),
        "module_payload_json": json.dumps(module_payload, ensure_ascii=False, indent=2),
    }
