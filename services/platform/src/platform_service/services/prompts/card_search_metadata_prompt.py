"""Post-publish prompt: generate search metadata for all module cards."""

from __future__ import annotations

import json
from typing import Any

CARD_SEARCH_METADATA_TEMPLATE_VERSION = 5


def render_human_message_for_module(
    *,
    module_context: dict[str, Any],
    card_payloads: list[dict[str, Any]],
) -> str:
    module_block = json.dumps(module_context, ensure_ascii=False, indent=2)
    cards_block = json.dumps(card_payloads, ensure_ascii=False, indent=2)
    return f"## MODULE CONTEXT ##\n{module_block}\n\n## CARDS TO INDEX ##\n{cards_block}"
