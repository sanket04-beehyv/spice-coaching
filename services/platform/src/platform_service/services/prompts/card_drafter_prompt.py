"""W-5 — Stage D card-drafter prompt (per-module-type variants).

Per Pipeline v3.3 §7 (with the architecture-reset adjustments). The drafter
takes a Stage C candidate plus its cited content_blocks and produces N
bilingual cards (3-7). The branching by module_type controls which fields
the LLM is asked to populate:

- initial_training → comprehensive instructional content with body_bn + body_en
  from comprehensive training manuals (detailed, procedural, foundational).
- refresher / digital_proficiency → body_bn + body_en (verbatim from source
  where possible; AI gap-fill only when one language is silent).
- content_update → previous_practice_*, current_practice_*,
  rationale_for_change_*, next_action_* fields (the delta-specific framing).

Snippet plumbing (registered fragments + `snippet_links`) was deleted with
the W-6 reviewer surface — it has no runtime resolver to back it. We do
NOT mention snippets in the prompt anymore: that wastes output tokens and
risks the LLM hallucinating `snippet_family_id` UUIDs that no validator
would catch.
"""

from __future__ import annotations

import json
from typing import Any

CARD_DRAFTER_TEMPLATE_ID = "v33-stage-d-card-drafter"
# v2: added cross-source coverage rule. When the candidate's cited blocks
# span multiple source_document_ids (a fused candidate from Stage 2b —
# clinical training manual + digital workflow guide), the card set MUST
# cite blocks from EACH source so the fused module's cards reflect the
# CHW activity from both angles. Without this rule the LLM defaulted to
# the larger pile and produced clinical-only cards for a fused module
# titled "X Counselling and Digital Service Delivery" (Family Planning
# experiment, 2026-05-09: 47 BRAC + 10 UHIS blocks → 5 cards, 0 citing
# UHIS). Cache entries from v1 will not be reused.
CARD_DRAFTER_TEMPLATE_VERSION = 2


_SYSTEM_BASE = """\
You are drafting bilingual learning cards for community health workers (CHWs)
in rural Bangladesh.

GROUND RULES (apply to every card):
- Each card covers ONE focused sub-topic. Do NOT cram multiple unrelated points
  into one card.
- Card content must come from the cited content_blocks below. Do NOT invent
  clinical content. If a sub-topic has no source coverage, drop the card.
- Bangla content (body_bn / title_bn / etc.) must use the EXACT vocabulary and
  tone of the cited Bangla blocks where available — these are the BRAC training
  manual phrasings the CHW already learned. Do NOT colloquialise.
- English content (body_en / title_en / etc.) must come from cited English blocks
  where available.
- If a field has no source coverage in one language but does in the other,
  AI-translate from the available source — and set field_flags[field_name].ai_translated = true
  so the reviewer knows to verify.
- Numerical values (BP 140/90, Hb 8 g/dL, etc.) are preserved verbatim across
  languages.
- Maximum {card_max_count} cards. If the candidate's scope produces more, pick
  the {card_max_count} most-important sub-topics.
- Minimum {card_min_count} cards. If you cannot draft that many from the cited
  blocks, return {{"insufficient_for_drafting": "<reason_code>"}} where reason_code
  is one of: no_actionable_content | single_concept_only | no_quiz_anchors |
  language_imbalance_severe.

CROSS-SOURCE COVERAGE (only when blocks span multiple sources):
Each cited block is tagged with its `source` label (e.g. `source=d1`,
`source=d2`). When the cited blocks span MULTIPLE source labels — meaning
this is a FUSED candidate that combines content from a clinical training
manual + a digital workflow guide (or two related sources) — your card
set MUST collectively cite blocks from EVERY source label present.
Specifically:
- At least one card per source label must include at least one
  `source_block_ids` entry from that source.
- The fused candidate exists because the clinical reasoning and the app
  workflow are halves of one CHW learning unit. Cards drawing only from
  one source defeat the purpose and produce a misleading module shape
  (fused title, single-source cards).
- If a source has only 1-2 cited blocks (an under-represented half), you
  must still produce at least one card grounded in those blocks even
  though the larger source has more material. Do NOT silently drop the
  smaller source.
- When all cited blocks are from a single source, this rule is a no-op
  and existing single-source drafting behaviour applies unchanged.

{module_type_rules}

OUTPUT SHAPE (strict JSON, no markdown fences, no commentary):
{{
  "cards": [
    {{
      "card_order": int (1-indexed),
      "title_en": "string or null",
      "title_bn": "string",
      {body_field_schema}
      "thresholds": [
        {{"label": "BP threshold", "value": "140/90 mmHg"}},
        ...
      ] (optional, for clinical thresholds; preserve numerical values verbatim),
      "figure_ref_block_id": "uuid string or null (cite a content_block of block_type=figure)",
      "source_block_ids": ["uuid string", ...] (every block that informed this card),
      "field_flags": {{
        "body_bn": {{"ai_translated": true}},
        ...
      }} (optional; only when AI gap-fill happened)
    }},
    ...
  ]
}}

If you cannot meet the minimum card count, return:
{{ "insufficient_for_drafting": "<reason_code>" }}

Only ONE of `cards` or `insufficient_for_drafting` should be populated.
"""


_INITIAL_TRAINING_RULES = """\
This is an `initial_training` module — comprehensive foundational training content
from official training manuals. Cards teach clinical knowledge, procedures, protocols,
and decision-making.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CRITICAL INSTRUCTION FOR INITIAL_TRAINING (read carefully):

For `initial_training` modules, you MUST draft cards from the provided source material.
DO NOT use the "insufficient_for_drafting" rejection codes unless the source is truly
empty or unusable. Specifically:

1. DO NOT reject with "no_actionable_content" — Instructional content that teaches
   CHWs how to classify conditions, recognize danger signs, apply referral criteria,
   perform procedures, or make clinical decisions IS actionable. The fact that content
   is educational/procedural does not make it non-actionable.

2. DO NOT reject with "single_concept_only" — Even if the candidate focuses on ONE
   clinical topic (e.g., "Correct ANC Referral by Risk Category" or "Recognising
   Postpartum Danger Signs"), you MUST break that topic into {card_min_count}-{card_max_count}
   cards by dividing it into logical sub-components such as:
   - Definition and background
   - Classification criteria or categories
   - Signs/symptoms to recognize
   - Decision-making thresholds
   - Referral criteria and timing
   - Counselling points or patient communication
   - Follow-up actions or documentation

3. If the cited blocks provide training content covering ANY of the above aspects,
   draft the minimum {card_min_count} cards. Only reject if the source blocks are
   literally empty, unreadable, or completely off-topic.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

How to structure initial_training cards:
- COMPREHENSIVE: Include all key details, steps, criteria, thresholds, and decision
  points from the source material. Do NOT abbreviate or summarize to brevity.
- INSTRUCTIONAL: Teach the CHW what to do, when, why, and how to recognize conditions.
- LOGICAL BREAKDOWN: Divide the candidate's topic into {card_min_count}-{card_max_count}
  focused sub-topics, each covering one aspect of the overall learning objective.
- DETAILED: Include classification criteria, danger signs, referral thresholds,
  counselling points, timing requirements, and follow-up actions.
- COMPLETE: Each card should be complete and understandable on its own, but together
  they form a comprehensive learning sequence on the topic.

The previous_practice_*, current_practice_*, rationale_for_change_*, next_action_*
fields are NOT used for initial_training cards (leave them out)."""

_REFRESHER_RULES = """\
This is a `refresher` module — current correct protocol. Cards present
present-tense protocol guidance from the cited blocks. The previous_practice_*,
current_practice_*, rationale_for_change_*, next_action_* fields are NOT used
for refresher cards (leave them out)."""

_DIGITAL_PROFICIENCY_RULES = """\
This is a `digital_proficiency` module — SPICE app workflow guidance. Cards
are procedural (steps, screen references) not clinical. Cite content_blocks of
block_type=figure for screenshots when they exist in the cited source. The
previous_practice_*, current_practice_*, rationale_for_change_*, next_action_*
fields are NOT used (leave them out)."""

_CONTENT_UPDATE_RULES = """\
This is a `content_update` module — a recent change in protocol. EVERY card MUST
populate the four delta fields:
- previous_practice_bn / previous_practice_en: what was done before the change
- current_practice_bn / current_practice_en: what is done now
- rationale_for_change_bn / rationale_for_change_en: why the change happened
- next_action_bn / next_action_en: what the CHW should do at the next visit

For content_update modules, body_bn / body_en MAY be empty — the four delta
fields carry the structured framing. Reviewer schema validation requires
previous_practice_bn AND current_practice_bn AND rationale_for_change_bn to
be non-empty for content_update cards."""


def _body_field_schema_for(module_type: str) -> str:
    if module_type == "content_update":
        return (
            '"body_bn": "string or null (may be empty for content_update; delta fields carry the framing)",\n'
            '      "body_en": "string or null",\n'
            '      "previous_practice_bn": "string (REQUIRED for content_update)",\n'
            '      "previous_practice_en": "string or null",\n'
            '      "current_practice_bn": "string (REQUIRED for content_update)",\n'
            '      "current_practice_en": "string or null",\n'
            '      "rationale_for_change_bn": "string (REQUIRED for content_update)",\n'
            '      "rationale_for_change_en": "string or null",\n'
            '      "next_action_bn": "string or null",\n'
            '      "next_action_en": "string or null",'
        )
    return (
        '"body_bn": "string (REQUIRED for initial_training / refresher / digital_proficiency)",\n'
        '      "body_en": "string or null",'
    )


def _module_type_rules_for(module_type: str) -> str:
    if module_type == "initial_training":
        return _INITIAL_TRAINING_RULES
    if module_type == "refresher":
        return _REFRESHER_RULES
    if module_type == "digital_proficiency":
        return _DIGITAL_PROFICIENCY_RULES
    if module_type == "content_update":
        return _CONTENT_UPDATE_RULES
    return _REFRESHER_RULES  # default


def render_system_prompt(
    *,
    module_type: str,
    card_min_count: int,
    card_max_count: int,
) -> str:
    """Render the per-module-type system prompt."""
    return _SYSTEM_BASE.format(
        card_min_count=card_min_count,
        card_max_count=card_max_count,
        module_type_rules=_module_type_rules_for(module_type),
        body_field_schema=_body_field_schema_for(module_type),
    )


def render_human_message(
    *,
    candidate: dict[str, Any],
    cited_blocks: list[dict[str, Any]],
) -> str:
    """Render the per-candidate human message.

    Each cited block carries `source_document_id`. We assign short source
    labels (`d1`, `d2`, ...) in first-seen order and tag every block with
    its label. When all blocks share one source the labelling is benign
    (LLM sees `source=d1` everywhere); when blocks span sources the
    cross-source coverage rule in the system prompt activates.
    """
    candidate_summary = {
        "proposed_title": candidate.get("proposed_title"),
        "scope_summary": candidate.get("scope_summary"),
        "proposed_module_type": candidate.get("proposed_module_type"),
        "estimated_card_count": candidate.get("estimated_card_count"),
        "previous_practice_summary": candidate.get("previous_practice_summary"),
        "current_practice_summary": candidate.get("current_practice_summary"),
        "rationale_summary": candidate.get("rationale_summary"),
    }
    head = json.dumps(
        {"candidate": {k: v for k, v in candidate_summary.items() if v is not None}},
        ensure_ascii=False,
        indent=2,
    )

    # Assign source labels in first-seen order.
    source_label: dict[str, str] = {}
    for blk in cited_blocks:
        sd = str(blk.get("source_document_id") or "unknown")
        if sd not in source_label:
            source_label[sd] = f"d{len(source_label) + 1}"

    body_lines = ["\n## CITED CONTENT BLOCKS ##"]
    if len(source_label) > 1:
        # Surface the per-source block count up-front so the LLM can plan
        # card distribution. Without this it can't tell which sources are
        # under-represented.
        per_source_counts = {label: 0 for label in source_label.values()}
        for blk in cited_blocks:
            sd = str(blk.get("source_document_id") or "unknown")
            per_source_counts[source_label[sd]] += 1
        body_lines.append(
            f"\n[multi-source candidate: blocks span {len(source_label)} sources — "
            f"counts {per_source_counts}. Per the cross-source coverage rule, your "
            f"card set MUST cite blocks from each source label.]"
        )

    for blk in cited_blocks:
        sd = str(blk.get("source_document_id") or "unknown")
        body_lines.append(
            f"\n[content_block_id={blk['content_block_id']} "
            f"source={source_label[sd]} "
            f"block_type={blk['block_type']} "
            f"language={blk.get('content_language', 'unknown')}]\n{blk['content_text']}"
        )
    return head + "\n" + "\n".join(body_lines)
