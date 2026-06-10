"""Stage 2-draft — module merge prompt (active / non-retired modules).

Given a newly drafted candidate (metadata + cards) and a bounded list of
existing modules (published or draft, not retired; latest per family), the
LLM decides whether any covers the same CHW behavioural unit with majority
card-content overlap. If so, it returns a merged card set where new content
wins on conflict.
"""

from __future__ import annotations

import json
from typing import Any

PUBLISHED_MODULE_MERGER_TEMPLATE_ID = "v33-stage-d-published-module-merger"
# v3: stricter match — majority existing-card overlap + high module content overlap;
# bias toward no match when uncertain.
PUBLISHED_MODULE_MERGER_TEMPLATE_VERSION = 3


SYSTEM_PROMPT = """\
You merge a NEWLY DRAFTED training module with at most ONE EXISTING module
(published or draft — not retired) only when they are substantively the SAME
training unit with majority overlapping card content.

A behavioural unit is ONE actionable topic the CHW must internalise (e.g.
"Correct ANC referral by risk category", not "all of ANC").

When to match (ALL must hold):
- The existing module and new candidate teach the same CHW behavioural unit.
- A MAJORITY of the existing module's cards cover the same substantive teaching
  points as cards in the new set (compare titles and bodies, not just domain).
- Overall card-body content overlap is HIGH across both sets — not keyword overlap
  or shared domain alone.
- If the existing module has semantically similar content overall to the new candidate, then match.
- If the existing module can be considered a subset of the new candidate in a way they are semantically similar, then match.
- If the existing module and the new candidate prescribes similar content, then match.

When NOT to match:
- Same domain but different topics (e.g. hypertension management vs diabetes).
- Only one or a minority of existing cards align with the new cards.
- You are uncertain — prefer NO match.
- These cards for a module are to be learnt by the CHW, so if the level of granularity is different, then do not match.

Rules:
1. Pick AT MOST ONE `matched_module_id` from the existing-modules list, or null if
   none meet the criteria above.
2. When matched: produce `merged_cards` combining BOTH card sets.
   - On overlapping topic/content, prefer the NEW candidate's card text.
   - Keep unique cards from the existing module that the new set does not
     replace.
   - Respect card count bounds: minimum {card_min_count}, maximum {card_max_count}.
3. When NOT matched: set `matched_module_id` to null and set `merged_cards`
   to the new candidate's cards unchanged (copy them exactly).
4. Do NOT invent clinical content. Preserve source_block_ids from inputs.
5. Bangla fields are canonical; English mirrors when present.
6. In `match_rationale`, briefly estimate overlap (e.g. "4/5 existing cards align").

Return STRICT JSON with this shape:
{{
  "matched_module_id": "uuid-string or null",
  "match_rationale": "1-3 sentences explaining match or why no match",
  "merged_cards": [
    {{
      "title_bn": "string",
      "title_en": "string or null",
      "body_bn": "string (refresher / initial_training / digital_proficiency)",
      "body_en": "string or null",
      "next_action_bn": "string or null",
      "next_action_en": "string or null",
      "previous_practice_bn": "string (content_update only)",
      "previous_practice_en": "string or null",
      "current_practice_bn": "string (content_update only)",
      "current_practice_en": "string or null",
      "rationale_for_change_bn": "string (content_update only)",
      "rationale_for_change_en": "string or null",
      "source_block_ids": ["uuid", ...],
      "thresholds": {{}} or null,
      "figure_ref_block_id": "uuid or null"
    }}
  ]
}}

Do not include markdown fences or commentary. Only the JSON object.
"""


def render_system_prompt(*, card_min_count: int, card_max_count: int) -> str:
    return SYSTEM_PROMPT.format(card_min_count=card_min_count, card_max_count=card_max_count)


def render_human_message(
    *,
    candidate: dict[str, Any],
    new_cards: list[dict[str, Any]],
    existing_modules: list[dict[str, Any]],
) -> str:
    candidate_payload = {
        "proposed_title": candidate.get("proposed_title"),
        "scope_summary": candidate.get("scope_summary"),
        "proposed_module_type": candidate.get("proposed_module_type"),
        "previous_practice_summary": candidate.get("previous_practice_summary"),
        "current_practice_summary": candidate.get("current_practice_summary"),
        "rationale_summary": candidate.get("rationale_summary"),
    }
    return (
        "## NEW CANDIDATE ##\n"
        f"{json.dumps(candidate_payload, ensure_ascii=False, indent=2)}\n\n"
        "## NEW CARDS ##\n"
        f"{json.dumps(new_cards, ensure_ascii=False, indent=2)}\n\n"
        "## EXISTING MODULES (pick at most one match) ##\n"
        f"{json.dumps(existing_modules, ensure_ascii=False, indent=2)}"
    )
