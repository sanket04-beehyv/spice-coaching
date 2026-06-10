"""Stage 2b — cross-source fusion prompt.

Operates on candidate metadata (titles + scopes + source_document_id) from
Stage 2a, NOT raw corpus content. The task is candidate-pairing: identify
which candidates from different source_document_ids cover the same CHW
behavioural unit (e.g., a clinical training manual's "ANC counselling and
danger signs" + a digital workflow manual's "Conducting ANC visits with
the App") and fuse them into one combined candidate citing source_provenance
from each constituent.

This is intentionally separate from MODULE_IDENTIFICATION (Stage 2a) which
extracts candidates from raw corpus. The shared-context-in-chunker
experiment showed that mixing extraction + alignment in one call collapses
output (45 single-source candidates → 8 single-source candidates). Fusion
is a metadata-pairing problem; it deserves its own narrow LLM call with
a small input (~3K tokens for ~45 candidates) and a precise output schema.
"""

from __future__ import annotations

import json
from typing import Any

CROSS_SOURCE_FUSER_TEMPLATE_ID = "v33-stage-c-cross-source-fuser"
# v1: initial implementation. Pairs candidates across source_document_ids
# whose titles+scopes describe the same CHW behavioural unit from different
# angles (typically clinical reasoning vs app workflow).
CROSS_SOURCE_FUSER_TEMPLATE_VERSION = 1


SYSTEM_PROMPT = """\
You are pairing already-extracted training-module candidates from MULTIPLE
source documents that train the SAME community health worker (CHW).

Each candidate has a title, a scope summary, and a `source_document_id`
identifying which source document it came from. Different source documents
typically cover the SAME CHW activity from DIFFERENT angles:

- A clinical training manual covers WHY and WHEN — decision criteria,
  danger signs, BP thresholds, danger-sign synthesis, referral rationale.
- A digital workflow manual covers HOW — which app screen, which field,
  which button, what the app does next.

Your task: identify candidate GROUPS where two or more candidates from
DIFFERENT source documents cover the SAME CHW behavioural unit.

What "same behavioural unit" means:
- Same patient population + same CHW activity (e.g., pre-eclampsia detection
  during ANC; postpartum danger-sign recognition; family-planning counselling)
- The clinical reasoning and the app workflow are halves of one learnable
  unit for the CHW. Pushing only the clinical half (no app context) or only
  the app half (no clinical reasoning) gives the CHW half the answer.

What is NOT a fusion group:
- Two candidates from the SAME source_document_id (those are intra-source
  duplicates, not cross-source fusions; do not merge them here)
- Two candidates whose topics are merely adjacent or share a domain
  (e.g., "Hypertension management" and "Diabetes management" — both NCDs
   but different CHW behavioural units; KEEP SEPARATE)
- Generic skill candidates that don't pair with a specific clinical/workflow
  counterpart (e.g., "Communication skills" stays alone unless paired with
  a workflow about a specific app communication feature)

Strict rules:
1. A candidate appears in AT MOST ONE fusion group.
2. Every fusion group MUST contain candidates from AT LEAST TWO DIFFERENT
   source_document_id values. A "group" of all-same-source candidates is
   invalid — leave them as-is.
3. Most candidates will NOT be in any fusion group. That is expected and
   correct. Single-source candidates are first-class outputs of this
   pipeline; fusion is for cases where the cross-source pairing is
   genuinely tight.
4. Bias toward LEAVING candidates UNPAIRED. False fusions degrade reviewer
   experience more than missed fusions.

For each fusion group, produce:
- candidate_ids: the list of candidate IDs being merged (≥2, from ≥2 distinct
  source documents)
- merged_title: a concise English title that captures the unified CHW unit
  (e.g., "Pre-eclampsia detection and digital referral" — combines clinical
  threshold + app referral workflow)
- merged_scope_summary: 3-5 sentences describing what the fused module
  teaches, drawing from BOTH the clinical reasoning AND the app workflow
- pairing_rationale: 1-2 sentences explaining why these candidates pair
  (what makes them the same CHW behavioural unit)

Return STRICT JSON with this top-level shape:
{
  "fusion_groups": [
    {
      "candidate_ids": ["uuid-1", "uuid-2"],
      "merged_title": "...",
      "merged_scope_summary": "...",
      "pairing_rationale": "..."
    },
    ...
  ]
}

If no candidates pair across sources, return `{"fusion_groups": []}`.
Do not include markdown fences or commentary. Only the JSON object.
"""


def render_system_prompt() -> str:
    """Stage 2b system prompt is invariant — no per-call substitutions."""
    return SYSTEM_PROMPT


def render_human_message(candidates: list[dict[str, Any]]) -> str:
    """Render the candidates list as the human-message payload.

    Each candidate is represented by id, source_document_id, title, and
    scope_summary — no raw corpus content. The LLM has only metadata to
    reason over, which is the entire point of Stage 2b's design (small
    bounded input, narrow alignment task).
    """
    payload = {
        "candidates": [
            {
                "id": str(c.get("id")),
                "source_document_id": str(c.get("source_document_id")),
                "title": c.get("proposed_title", ""),
                "scope_summary": c.get("scope_summary", ""),
            }
            for c in candidates
        ]
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
