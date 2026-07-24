"""Variable builders for card search metadata prompt."""

from __future__ import annotations

import json
from typing import Any

from platform_service.config import get_settings
from platform_service.services.prompts.symbol_verbalization import (
    render_locale_list_map_field_schema,
    render_locale_synonym_map_field_schema,
)


def build_card_search_metadata_variables(
    *,
    max_retrieval_hints: int,
    max_keywords: int,
    max_synonyms: int,
    max_questions: int,
    module_context: dict[str, Any],
    card_payloads: list[dict[str, Any]],
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
) -> dict[str, str]:
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    region_context = deployment_region_context or settings.deployment_region_context
    return {
        "deployment_region_context": region_context,
        "max_retrieval_hints": str(max_retrieval_hints),
        "max_keywords": str(max_keywords),
        "max_synonyms": str(max_synonyms),
        "max_questions": str(max_questions),
        "retrieval_hints_field_schema": render_locale_list_map_field_schema(
            "retrieval_hints",
            primary_locale=primary_locale,
            max_items=max_retrieval_hints,
            description="search scenarios for this card",
        ),
        "keywords_field_schema": render_locale_list_map_field_schema(
            "keywords",
            primary_locale=primary_locale,
            max_items=max_keywords,
            description="terms and clinical vocabulary",
        ),
        "synonyms_field_schema": render_locale_synonym_map_field_schema(
            "synonyms",
            primary_locale=primary_locale,
            max_items=max_synonyms,
            description="abbreviation expansions in primary language",
        ),
        "questions_field_schema": render_locale_list_map_field_schema(
            "questions",
            primary_locale=primary_locale,
            max_items=max_questions,
            description="FAQ-style questions this card answers",
        ),
        "module_context_json": json.dumps(module_context, ensure_ascii=False, indent=2),
        "cards_json": json.dumps(card_payloads, ensure_ascii=False, indent=2),
    }
