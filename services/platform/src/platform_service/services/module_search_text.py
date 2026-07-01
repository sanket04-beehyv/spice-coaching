"""Assemble searchable plain text from a module row (embedding + lexical retrieval)."""

from __future__ import annotations

from typing import Any

from mc_foundation.locale import LOCALIZED_CARD_TEXT_FIELDS, LOCALIZED_SEARCH_METADATA_LIST_FIELDS

from platform_service.config import Settings, get_settings
from platform_service.db.models.module import Module
from platform_service.localized import (
    deployment_locales,
    migrate_legacy_card,
    migrate_legacy_search_metadata,
    primary_text,
)
from platform_service.services.card_body_text import card_body_plain_text


def _localized_values(
    localized: dict[str, Any] | None,
    *,
    settings: Settings | None = None,
) -> list[str]:
    out: list[str] = []
    p = primary_text(localized, settings=settings)
    if p:
        out.append(p)
    if out:
        return out
    if isinstance(localized, dict):
        for value in localized.values():
            if isinstance(value, str) and value.strip():
                out.append(value.strip())
    return out


def _list_values_from_localized_map(localized: dict[str, Any] | None) -> list[str]:
    if not isinstance(localized, dict):
        return []
    parts: list[str] = []
    seen: set[str] = set()
    for values in localized.values():
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text and text not in seen:
                seen.add(text)
                parts.append(text)
    return parts


def card_metadata_text_for_search(metadata: dict[str, Any] | None) -> list[str]:
    """Extract per-card lexical metadata fields for BM25 / embedding indexing."""
    if not metadata:
        return []

    parts: list[str] = []
    seen: set[str] = set()

    def _add(value: str | None) -> None:
        if not value:
            return
        text = value.strip()
        if not text or text in seen:
            return
        seen.add(text)
        parts.append(text)

    migrated = migrate_legacy_search_metadata(dict(metadata))
    for key in LOCALIZED_SEARCH_METADATA_LIST_FIELDS:
        raw = migrated.get(key)
        if isinstance(raw, dict):
            for item in _list_values_from_localized_map(raw):
                _add(item)

    synonyms = metadata.get("synonyms_en")
    if isinstance(synonyms, dict):
        for abbrev, expanded in synonyms.items():
            if isinstance(abbrev, str):
                _add(abbrev)
            if isinstance(expanded, str):
                _add(expanded)

    return parts


def metadata_text_for_search(metadata: dict[str, Any] | None) -> list[str]:
    """Extract lexical and structured metadata fields for BM25 / embedding indexing."""
    if not metadata:
        return []

    parts: list[str] = []
    seen: set[str] = set()

    def _add(value: str | None) -> None:
        if not value:
            return
        text = value.strip()
        if not text or text in seen:
            return
        seen.add(text)
        parts.append(text)

    migrated = migrate_legacy_search_metadata(dict(metadata))
    for key in LOCALIZED_SEARCH_METADATA_LIST_FIELDS:
        raw = migrated.get(key)
        if isinstance(raw, dict):
            for item in _list_values_from_localized_map(raw):
                _add(item)

    for key in ("topic_tags", "clinical_conditions"):
        raw = metadata.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, str):
                _add(item)

    synonyms = metadata.get("synonyms_en")
    if isinstance(synonyms, dict):
        for abbrev, expanded in synonyms.items():
            if isinstance(abbrev, str):
                _add(abbrev)
            if isinstance(expanded, str):
                _add(expanded)

    return parts


def module_text_for_search(module: Module, *, settings: Settings | None = None) -> str:
    """Concatenate the module's title and card text into a single search input.

    Order: title → description → search metadata → each card's localized fields
    in card order.
    """
    s = settings or get_settings()
    parts: list[str] = []
    parts.extend(_localized_values(module.title_localized, settings=s))
    parts.extend(_localized_values(module.description_localized, settings=s))
    parts.extend(metadata_text_for_search(module.search_metadata_jsonb))
    cards = (module.module_json or {}).get("cards", [])
    primary = deployment_locales(s)
    for card in cards:
        if not isinstance(card, dict):
            continue
        migrated_card = migrate_legacy_card(dict(card), primary=primary)
        for field in LOCALIZED_CARD_TEXT_FIELDS:
            value = migrated_card.get(field)
            if not value:
                continue
            if field == "body" and isinstance(value, dict):
                for locale_value in value.values():
                    text = card_body_plain_text(locale_value)
                    if text:
                        parts.append(text)
            elif isinstance(value, dict):
                parts.extend(_localized_values(value, settings=s))
            else:
                parts.append(str(value))
        parts.extend(card_metadata_text_for_search(card.get("search_metadata")))
    return "\n".join(parts)
