"""Post-publish prompt: map a drafted module to referral-domain behavioural_gap codes."""

from __future__ import annotations

import json
from typing import Any

GAP_CLASSIFICATION_TEMPLATE_ID = "post-publish-gap-classification"
GAP_CLASSIFICATION_TEMPLATE_VERSION = 1

_SYSTEM_PROMPT = """\
You classify community-health-worker (CHW) training modules against a fixed
registry of referral-domain compliance gaps. Each registry entry describes a
referral practice failure detected at visit time (e.g. missed referral, wrong
destination, incorrect urgency). The module's primary teaching gap is already
assigned separately; you are selecting secondary referral associations only.

Rules:
- Select ONLY gap_codes from the supplied registry. Do not invent codes.
- Multi-label: return every registry gap that genuinely applies to what the
  module teaches or corrects. Return an empty list when no registry gap fits.
- Do NOT select codes starting with `module_primary_gap_` (those are
  module-specific primary gaps, not registry entries).
- Prefer precision over recall: include a gap only when the module content
  clearly addresses that failure pattern.
- At most {max_associations} associated_gap_codes.

Return STRICT JSON with this shape:
{{
  "associated_gap_codes": ["gap_code_from_registry", ...],
  "rationale": "1-3 sentences for clinical reviewers explaining the mapping"
}}

Do not include markdown fences or commentary. Only the JSON object.
"""


def render_system_prompt(*, max_associations: int) -> str:
    return _SYSTEM_PROMPT.format(max_associations=max_associations)


def render_human_message(
    *,
    module_payload: dict[str, Any],
    registry_gaps: list[dict[str, str]],
) -> str:
    gaps_block = json.dumps(registry_gaps, ensure_ascii=False, indent=2)
    module_block = json.dumps(module_payload, ensure_ascii=False, indent=2)
    return f"## BEHAVIOURAL GAP REGISTRY ##\n{gaps_block}\n\n## MODULE TO CLASSIFY ##\n{module_block}"
