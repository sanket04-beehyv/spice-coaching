"""Card payload normalisation — draft validation, runtime projection, ORM mapping."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from mc_foundation.locale import localized_primary_text

from platform_service.config import get_settings
from platform_service.localized import migrate_legacy_card
from platform_service.services.card_body_text import card_body_is_nonempty
from platform_service.services.llm_text_utils import strip_markdown_formatting

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

_LOCALIZED_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("title", "title_localized"),
    ("body", "body_localized"),
    ("previous_practice", "previous_practice_localized"),
    ("current_practice", "current_practice_localized"),
    ("rationale_for_change", "rationale_for_change_localized"),
    ("next_action", "next_action_localized"),
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

    def _sanitize_localized(value: Any) -> Any:
        # Draft cards may contain localized strings; enforce plain text so
        # formatting-only values don't pass required-field checks.
        if isinstance(value, str):
            return strip_markdown_formatting(value)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                out[k] = strip_markdown_formatting(v) if isinstance(v, str) else v
            return out
        return value

    for field in (
        "title",
        "body",
        "previous_practice",
        "current_practice",
        "rationale_for_change",
        "next_action",
    ):
        if field in card:
            card[field] = _sanitize_localized(card.get(field))

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


def _parse_uuid(raw: Any) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _parse_uuid_list(raw: Any) -> list[uuid.UUID] | None:
    if not raw:
        return None
    if not isinstance(raw, list):
        return None
    out: list[uuid.UUID] = []
    for item in raw:
        parsed = _parse_uuid(item)
        if parsed is not None:
            out.append(parsed)
    return out or None


def card_dict_to_row_fields(card: dict[str, Any]) -> dict[str, Any]:
    """Map an API/runtime card dict into ``ModuleCard`` column kwargs."""
    settings = get_settings()
    primary = settings.deployment_primary_locale
    normalized = migrate_legacy_card(dict(card), primary=primary)

    def _sanitize_localized(value: Any) -> Any:
        # Localized fields are typically {locale: str|rich_text}; we only
        # strip markdown formatting from plain strings and leave rich-text JSON as-is.
        if isinstance(value, str):
            return strip_markdown_formatting(value)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for k, v in value.items():
                if isinstance(v, str):
                    out[k] = strip_markdown_formatting(v)
                else:
                    out[k] = v
            return out
        return value

    row_fields: dict[str, Any] = {}
    for src_field, dest_field in _LOCALIZED_FIELD_MAP:
        value = normalized.get(src_field)
        if value is not None:
            row_fields[dest_field] = _sanitize_localized(value)
    thresholds = normalized.get("thresholds")
    if thresholds is not None:
        row_fields["thresholds_jsonb"] = thresholds
    block_ids = _parse_uuid_list(normalized.get("source_block_ids"))
    if block_ids is not None:
        row_fields["source_block_ids"] = block_ids
    figure_ref = _parse_uuid(normalized.get("figure_ref_block_id"))
    if figure_ref is not None:
        row_fields["figure_ref_block_id"] = figure_ref
    search_metadata = normalized.get("search_metadata")
    if search_metadata is not None:
        row_fields["search_metadata_jsonb"] = search_metadata
    attachments = normalized.get("attachments")
    if attachments is not None:
        row_fields["attachments_jsonb"] = attachments
    field_flags = normalized.get("field_flags_jsonb") or normalized.get("field_flags")
    if field_flags is not None:
        row_fields["field_flags_jsonb"] = field_flags
    return row_fields


def card_row_to_dict(row: Any) -> dict[str, Any]:
    """Map a ``ModuleCard`` ORM row into the admin/sync card dict shape."""
    payload: dict[str, Any] = {
        "id": str(row.id),
        "card_family_id": str(row.card_family_id),
        "card_order": row.card_order,
        "title": row.title_localized,
    }
    if row.body_localized is not None:
        payload["body"] = row.body_localized
    if row.previous_practice_localized is not None:
        payload["previous_practice"] = row.previous_practice_localized
    if row.current_practice_localized is not None:
        payload["current_practice"] = row.current_practice_localized
    if row.rationale_for_change_localized is not None:
        payload["rationale_for_change"] = row.rationale_for_change_localized
    if row.next_action_localized is not None:
        payload["next_action"] = row.next_action_localized
    if row.thresholds_jsonb is not None:
        payload["thresholds"] = row.thresholds_jsonb
    if row.source_block_ids:
        payload["source_block_ids"] = [str(bid) for bid in row.source_block_ids]
    if row.figure_ref_block_id is not None:
        payload["figure_ref_block_id"] = str(row.figure_ref_block_id)
    if row.search_metadata_jsonb is not None:
        payload["search_metadata"] = row.search_metadata_jsonb
    if row.attachments_jsonb is not None:
        payload["attachments"] = row.attachments_jsonb
    if row.field_flags_jsonb is not None:
        payload["field_flags_jsonb"] = row.field_flags_jsonb
    return payload
