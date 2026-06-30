"""Post-publish prompt: generate search metadata for all module cards."""

from __future__ import annotations

import json
from typing import Any

from platform_service.config import get_settings
from platform_service.services.prompts.symbol_verbalization import render_locale_list_map_field_schema

CARD_SEARCH_METADATA_TEMPLATE_ID = "post-publish-card-search-metadata"
# v4: monolingual deployment — primary locale only.
CARD_SEARCH_METADATA_TEMPLATE_VERSION = 4

_SYSTEM_PROMPT = """\
You generate search metadata for ALL cards in a community-health-worker (CHW)
training module in {deployment_region_context}. The metadata helps health workers
find each specific card when they type natural clinical questions or keywords.

Given module context plus an array of cards (title, body, practice fields),
produce lexical enrichment for every card, scoped only to what each card covers.

Rules (apply per card):
- retrieval_hints: short natural-language search scenarios a CHW might type to
  find this card (≤ {max_retrieval_hints}). Include symptom-duration patterns
  and threshold questions when relevant.
- keywords: short terms, abbreviations, and clinical vocabulary from or implied
  by the card (≤ {max_keywords}).
- synonyms_en: map abbreviations to expanded forms (e.g. "ARI" → "acute respiratory infection")
  (≤ {max_synonyms} entries).
- questions: explicit FAQ-style questions this card answers
  (≤ {max_questions}). Distinct from retrieval hints — full questions, not fragments.

Return STRICT JSON with this shape:
{{
  "schema_version": 1,
  "cards": [
    {{
      "card_index": 0,
{retrieval_hints_field_schema}
{keywords_field_schema}
      "synonyms_en": {{"ABBREV": "expanded form"}},
{questions_field_schema}
    }}
  ]
}}

Include one entry per input card. Each entry MUST include the matching card_index.
Do not include markdown fences or commentary. Only the JSON object.
"""


def render_system_prompt(
    *,
    max_retrieval_hints: int,
    max_keywords: int,
    max_synonyms: int,
    max_questions: int,
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
) -> str:
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    region_context = deployment_region_context or settings.deployment_region_context

    return _SYSTEM_PROMPT.format(
        deployment_region_context=region_context,
        max_retrieval_hints=max_retrieval_hints,
        max_keywords=max_keywords,
        max_synonyms=max_synonyms,
        max_questions=max_questions,
        retrieval_hints_field_schema=render_locale_list_map_field_schema(
            "retrieval_hints",
            primary_locale=primary_locale,
            max_items=max_retrieval_hints,
            description="search scenarios for this card",
        ),
        keywords_field_schema=render_locale_list_map_field_schema(
            "keywords",
            primary_locale=primary_locale,
            max_items=max_keywords,
            description="terms and clinical vocabulary",
        ),
        questions_field_schema=render_locale_list_map_field_schema(
            "questions",
            primary_locale=primary_locale,
            max_items=max_questions,
            description="FAQ-style questions this card answers",
        ),
    )


def render_human_message_for_module(
    *,
    module_context: dict[str, Any],
    card_payloads: list[dict[str, Any]],
) -> str:
    module_block = json.dumps(module_context, ensure_ascii=False, indent=2)
    cards_block = json.dumps(card_payloads, ensure_ascii=False, indent=2)
    return f"## MODULE CONTEXT ##\n{module_block}\n\n## CARDS TO INDEX ##\n{cards_block}"
