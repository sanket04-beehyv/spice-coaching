"""Nightly prompt: synthesize chat FAQ chips from clustered questions."""

from __future__ import annotations

import json
from typing import Any

from mc_foundation.locale import locale_display_name

from platform_service.config import get_settings
from platform_service.services.prompts.symbol_verbalization import render_locale_map_field_schema

CHAT_FAQ_TEMPLATE_ID = "nightly-chat-faq-synthesis"
# v3: monolingual deployment — primary locale only.
CHAT_FAQ_TEMPLATE_VERSION = 3

_SYSTEM_PROMPT = """\
You generate FAQ suggestion chips for a community-health-worker (CHW) field chat
experience in {deployment_region_context}. You receive clusters of real questions
CHWs typed in the app. Each cluster groups paraphrases of the same underlying
clinical topic.

Rules:
- Produce exactly {target_count} FAQ items (or fewer only if fewer clusters were provided).
- For each item, write one natural question in the `question` map — phrasing a CHW
  might tap as a suggestion chip ({primary_locale_label} required).
- Base wording only on themes present in the cluster members; do not invent clinical
  facts, thresholds, or protocols not implied by the inputs.
- Deduplicate overlapping topics across clusters.
- Set source_cluster_index to the cluster index from the input (0-based).

Return STRICT JSON with this shape:
{{
  "faqs": [
    {{
{question_field_schema}
      "source_cluster_index": 0
    }}
  ]
}}

Do not include markdown fences or commentary. Only the JSON object.
"""


def render_system_prompt(
    *,
    target_count: int,
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
) -> str:
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    region_context = deployment_region_context or settings.deployment_region_context
    primary_label = locale_display_name(primary_locale)

    return _SYSTEM_PROMPT.format(
        target_count=target_count,
        deployment_region_context=region_context,
        primary_locale_label=primary_label,
        question_field_schema=render_locale_map_field_schema(
            "question",
            primary_locale=primary_locale,
            primary_required=True,
            description="FAQ chip text",
        ),
    )


def render_human_message(*, clusters_payload: list[dict[str, Any]]) -> str:
    block = json.dumps(clusters_payload, ensure_ascii=False, indent=2)
    return f"## CLUSTERED CHW QUESTIONS ##\n{block}"
