"""Card payload normalisation — draft validation and runtime projection."""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

_RUNTIME_CARD_KEYS = (
    "title_bn",
    "title_en",
    "body_bn",
    "body_en",
    "previous_practice_bn",
    "previous_practice_en",
    "current_practice_bn",
    "current_practice_en",
    "rationale_for_change_bn",
    "rationale_for_change_en",
    "next_action_bn",
    "next_action_en",
    "thresholds",
    "source_block_ids",
    "figure_ref_block_id",
)


def normalise_draft_card(
    raw: dict[str, Any],
    *,
    module_type: str,
    valid_block_ids: set[uuid.UUID],
) -> dict[str, Any] | None:
    """Validate and clean one LLM draft card. Returns None when invalid."""
    if module_type in ("refresher", "digital_proficiency", "initial_training"):
        if not (raw.get("body_bn") or "").strip():
            logger.warning(
                "Rejecting card: %s requires body_bn",
                module_type,
            )
            return None
    elif module_type == "content_update":
        for f in ("previous_practice_bn", "current_practice_bn", "rationale_for_change_bn"):
            if not (raw.get(f) or "").strip():
                logger.warning("Rejecting content_update card: missing required field %r", f)
                return None
    if not (raw.get("title_bn") or "").strip():
        logger.warning("Rejecting card: missing title_bn")
        return None

    block_ids_raw = raw.get("source_block_ids", []) or []
    valid_blocks: list[str] = []
    for bid_raw in block_ids_raw:
        try:
            bid = uuid.UUID(str(bid_raw))
        except (TypeError, ValueError):
            continue
        if bid in valid_block_ids:
            valid_blocks.append(str(bid))
    if not valid_blocks:
        logger.warning("Rejecting card: no valid source_block_ids")
        return None
    raw["source_block_ids"] = valid_blocks

    fig_raw = raw.get("figure_ref_block_id")
    if fig_raw:
        try:
            fig_uuid = uuid.UUID(str(fig_raw))
        except (TypeError, ValueError):
            raw["figure_ref_block_id"] = None
        else:
            if fig_uuid not in valid_block_ids:
                raw["figure_ref_block_id"] = None
            else:
                raw["figure_ref_block_id"] = str(fig_uuid)

    return raw


def project_runtime_card(card: dict[str, Any]) -> dict[str, Any]:
    """Project a drafter card dict into the runtime payload shape."""
    return {k: card[k] for k in _RUNTIME_CARD_KEYS if k in card and card[k] is not None}
