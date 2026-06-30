"""Shared helpers for locale-keyed test fixtures."""

from __future__ import annotations

from typing import Any

PRIMARY = "bn"


def loc(primary_text: str) -> dict[str, str]:
    """Build a LocalizedString dict for the default primary locale (bn)."""
    return {PRIMARY: primary_text}


def loc_body(
    primary_text: str | dict[str, Any] | list[Any],
) -> dict[str, Any] | dict[str, str]:
    """Build a localized body field (plain text, ProseMirror doc, or block list)."""
    if isinstance(primary_text, (dict, list)):
        return {PRIMARY: primary_text}
    return loc(primary_text)


def loc_options(primary: list[str]) -> dict[str, list[str]]:
    """Build LocalizedOptions for quiz fixtures."""
    return {PRIMARY: primary}


def loc_list(primary: list[str]) -> dict[str, list[str]]:
    """Build a locale-keyed list map (search metadata keywords, etc.)."""
    return loc_options(primary)


def refresher_card(
    *,
    title: str,
    body: str | dict[str, Any] | list[Any],
    next_action: str = "পদক্ষেপ।",
    source_block_ids: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Minimal refresher card payload using localized field names."""
    card: dict[str, Any] = {
        "title": loc(title),
        "body": loc_body(body),
        "next_action": loc(next_action),
    }
    if source_block_ids is not None:
        card["source_block_ids"] = source_block_ids
    card.update(extra)
    return card


def primary_from_response(data: dict[str, Any], field: str = "title") -> str:
    """Read deployment-primary text from an API LocalizedString field."""
    value = data[field]
    if isinstance(value, dict):
        return value.get(PRIMARY, "")
    return str(value)
