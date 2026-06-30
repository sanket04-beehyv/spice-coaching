"""Card payload normalisation — draft validation and runtime projection."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from mc_foundation.locale import localized_primary_text

from platform_service.config import get_settings
from platform_service.localized import migrate_legacy_card
from platform_service.services.card_body_text import card_body_is_nonempty

logger = logging.getLogger(__name__)

_RUNTIME_CARD_KEYS = (
    "title",
    "body",
    "previous_practice",
    "current_practice",
    "rationale_for_change",
    "next_action",
    "thresholds",
    "source_block_ids",
    "figure_ref_block_id",
)


def _field_text(raw: dict[str, Any], field: str, *, primary_locale: str) -> str:
    value = raw.get(field)
    if isinstance(value, dict):
        text = localized_primary_text(value, primary_locale)
        return text or ""
    return ""


def normalise_draft_card(
    raw: dict[str, Any],
    *,
    module_type: str,
    valid_block_ids: set[uuid.UUID],
) -> dict[str, Any] | None:
    """Validate and clean one LLM draft card. Returns None when invalid."""
    settings = get_settings()
    primary = settings.deployment_primary_locale
    card = migrate_legacy_card(dict(raw), primary=primary)

    if module_type in ("refresher", "digital_proficiency", "initial_training"):
        body_value = card.get("body")
        if isinstance(body_value, dict):
            body_for_check = localized_primary_text(body_value, primary)
        else:
            body_for_check = body_value
        if not card_body_is_nonempty(body_for_check):
            logger.warning(
                "Rejecting card: %s requires body in primary locale %r",
                module_type,
                primary,
            )
            return None
    elif module_type == "content_update":
        for field in ("previous_practice", "current_practice", "rationale_for_change"):
            if not _field_text(card, field, primary_locale=primary):
                logger.warning(
                    "Rejecting content_update card: missing required field %r in %r",
                    field,
                    primary,
                )
                return None
    if not _field_text(card, "title", primary_locale=primary):
        logger.warning("Rejecting card: missing title in primary locale %r", primary)
        return None

    block_ids_raw = card.get("source_block_ids", []) or []
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
    card["source_block_ids"] = valid_blocks

    fig_raw = card.get("figure_ref_block_id")
    if fig_raw:
        try:
            fig_uuid = uuid.UUID(str(fig_raw))
        except (TypeError, ValueError):
            card["figure_ref_block_id"] = None
        else:
            if fig_uuid not in valid_block_ids:
                card["figure_ref_block_id"] = None
            else:
                card["figure_ref_block_id"] = str(fig_uuid)

    return card


def project_runtime_card(card: dict[str, Any]) -> dict[str, Any]:
    """Project a drafter card dict into the runtime payload shape."""
    return {k: card[k] for k in _RUNTIME_CARD_KEYS if k in card and card[k] is not None}
