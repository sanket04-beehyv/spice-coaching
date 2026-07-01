"""Post-publish prompt: map a drafted module to assessment-due topic keys."""

from __future__ import annotations

import json
from typing import Any

ASSESSMENT_TOPIC_CLASSIFICATION_TEMPLATE_ID = "post-publish-assessment-topic-classification"
ASSESSMENT_TOPIC_CLASSIFICATION_TEMPLATE_VERSION = 1

_SYSTEM_PROMPT = """\
You classify community-health-worker (CHW) training modules against a fixed
registry of assessment-due patient condition topics. These topics correspond to
patients due for follow-up visits today (RMNCH, ICCM, child health).

Rules:
- Select ONLY assessment_topic keys from the supplied allow-list. Do not invent keys.
- Multi-label: return every topic that genuinely applies to what the module teaches.
  Return an empty list when no topic fits.
- Prefer precision over recall: include a topic only when module content clearly
  addresses that clinical follow-up condition.
- Choose exactly one primary_topic from assessment_topics (the best single match).
- At most {max_topics} assessment_topics.

Return STRICT JSON with this shape:
{{
  "assessment_topics": ["topic_key_from_allow_list", ...],
  "primary_topic": "one_of_assessment_topics",
  "rationale": "1-3 sentences for clinical reviewers explaining the mapping"
}}

Do not include markdown fences or commentary. Only the JSON object.
"""


def render_system_prompt(*, max_topics: int, allowed_topics: list[str]) -> str:
    topics_line = ", ".join(allowed_topics)
    return _SYSTEM_PROMPT.format(max_topics=max_topics) + f"\n\nAllowed topics: {topics_line}"


def render_human_message(
    *,
    module_payload: dict[str, Any],
    search_metadata: dict[str, Any] | None,
    allowed_topics: list[str] | None = None,
) -> str:
    metadata_block = json.dumps(search_metadata or {}, ensure_ascii=False, indent=2)
    module_block = json.dumps(module_payload, ensure_ascii=False, indent=2)
    allow_block = json.dumps(allowed_topics or [], ensure_ascii=False, indent=2)
    return (
        f"## ALLOWED ASSESSMENT TOPICS ##\n{allow_block}\n\n"
        f"## SEARCH METADATA ##\n{metadata_block}\n\n"
        f"## MODULE TO CLASSIFY ##\n{module_block}"
    )
