"""Validate ``module_json`` card body fields before admin module create/edit."""

from __future__ import annotations

import copy
from typing import Any

from platform_service.db.validators import ValidationError
from platform_service.services.card_body_text import is_rich_text_body

_BODY_FIELDS = ("body",)


def _validate_localized_body_field(value: Any, *, location: str) -> None:
    if value is None:
        return
    if isinstance(value, str) or is_rich_text_body(value):
        _validate_body_field(value, location=location)
        return
    if isinstance(value, dict):
        for locale, locale_value in value.items():
            _validate_body_field(locale_value, location=f"{location}.{locale}")
        return
    _validate_body_field(value, location=location)


def _validate_body_field(value: Any, *, location: str) -> None:
    if value is None:
        return
    if isinstance(value, str):
        return
    if is_rich_text_body(value):
        return
    raise ValidationError(
        "invalid_card_body",
        f"{location}: body field must be a string, null, or rich-text blocks "
        "(ProseMirror doc, block object, or list of blocks)",
    )


def validate_module_card_bodies(module_json: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate card body shapes and return a deep copy (values unchanged)."""
    if module_json is None:
        return None

    out = copy.deepcopy(module_json)
    cards = out.get("cards")
    if cards is None:
        return out
    if not isinstance(cards, list):
        raise ValidationError("invalid_module_cards", "module_json.cards must be a list")

    for idx, card in enumerate(cards):
        if not isinstance(card, dict):
            raise ValidationError("invalid_module_cards", f"cards[{idx}] must be an object")
        for field in _BODY_FIELDS:
            _validate_localized_body_field(card.get(field), location=f"cards[{idx}].{field}")

    return out
