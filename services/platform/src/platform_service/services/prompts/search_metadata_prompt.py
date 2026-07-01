"""Post-publish prompt: generate search metadata for module retrieval."""

from __future__ import annotations

import json
from typing import Any

from platform_service.config import get_settings
from platform_service.services.prompts.symbol_verbalization import render_locale_list_map_field_schema

SEARCH_METADATA_TEMPLATE_ID = "post-publish-search-metadata"
# v3: monolingual deployment — primary locale only.
SEARCH_METADATA_TEMPLATE_VERSION = 3

_SYSTEM_PROMPT = """\
You generate search metadata for community-health-worker (CHW) training modules
in {deployment_region_context}. The metadata helps health workers find the right
module when they type natural clinical questions or keywords — not just when
they know the exact module title.

Given a drafted module (title, description, domain, and card summaries), produce
lexical enrichment plus structured tags.

Rules:
- keywords: short terms, abbreviations, and clinical vocabulary
  (≤ {max_keywords}).
- search_phrases: natural-language questions or scenarios a CHW might type
  (≤ {max_search_phrases}). Include symptom-duration patterns, threshold questions,
  and referral scenarios when relevant.
- synonyms_en: map abbreviations to expanded forms (e.g. "ARI" → "acute respiratory infection").
- topic_tags: broad topical labels using snake_case (e.g. respiratory, child_health).
- clinical_conditions: specific conditions or syndromes the module addresses.
- audience: always "chw_field_worker".
- rationale: one sentence for clinical reviewers explaining the metadata choices.

Return STRICT JSON with this shape:
{{
  "schema_version": 1,
{keywords_field_schema}
{search_phrases_field_schema}
  "synonyms_en": {{"ABBREV": "expanded form"}},
  "topic_tags": ["..."],
  "clinical_conditions": ["..."],
  "audience": "chw_field_worker",
  "rationale": "..."
}}

Do not include markdown fences or commentary. Only the JSON object.
"""


def render_system_prompt(
    *,
    max_keywords: int,
    max_search_phrases: int,
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
) -> str:
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    region_context = deployment_region_context or settings.deployment_region_context

    return _SYSTEM_PROMPT.format(
        deployment_region_context=region_context,
        max_keywords=max_keywords,
        max_search_phrases=max_search_phrases,
        keywords_field_schema=render_locale_list_map_field_schema(
            "keywords",
            primary_locale=primary_locale,
            max_items=max_keywords,
            description="short terms and clinical vocabulary",
        ),
        search_phrases_field_schema=render_locale_list_map_field_schema(
            "search_phrases",
            primary_locale=primary_locale,
            max_items=max_search_phrases,
            description="natural-language questions or scenarios",
        ),
    )


def render_human_message(*, module_payload: dict[str, Any]) -> str:
    module_block = json.dumps(module_payload, ensure_ascii=False, indent=2)
    return f"## MODULE TO INDEX ##\n{module_block}"
