"""Helpers for locale-keyed content in platform services and DB layers."""

from __future__ import annotations

from typing import Any

from mc_contracts.localized import LocalizedOptions, LocalizedString
from mc_foundation.locale import (
    LOCALIZED_CARD_TEXT_FIELDS,
    LOCALIZED_SEARCH_METADATA_LIST_FIELDS,
    build_localized_string,
    localized_primary_text,
)

from platform_service.config import Settings, get_settings

# Legacy bilingual suffixes used before the localized schema migration.
_LEGACY_PRIMARY_SUFFIX = "_bn"
_LEGACY_MIRROR_SUFFIX = "_en"


def deployment_locales(settings: Settings | None = None) -> str:
    """Return the deployment primary locale code."""
    s = settings or get_settings()
    return s.deployment_primary_locale


def to_localized_string(
    primary_text: str | None,
    *,
    settings: Settings | None = None,
) -> LocalizedString:
    primary = deployment_locales(settings)
    return build_localized_string(primary, primary_text=primary_text)


def primary_text(localized: LocalizedString | None, *, settings: Settings | None = None) -> str | None:
    primary = deployment_locales(settings)
    return localized_primary_text(localized, primary)


def candidate_description_localized(
    candidate: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> LocalizedString | None:
    """Resolve candidate description from localized map or legacy suffix keys."""
    s = settings or get_settings()
    primary = deployment_locales(s)
    data = dict(candidate)
    if "description" not in data:
        migrate_legacy_suffix_field(data, "description", primary=primary)
    desc = data.get("description")
    if isinstance(desc, dict):
        localized = to_localized_string(primary_text(desc, settings=s), settings=s)
        return localized or None
    scope = candidate.get("scope_summary")
    if isinstance(scope, str) and scope.strip():
        return to_localized_string(scope.strip(), settings=s) or None
    return None


def _pop_legacy_locale_value(
    data: dict[str, Any],
    field: str,
    locale: str | None,
    *,
    fallback_suffix: str | None = None,
) -> Any:
    """Pop ``field_{locale}``, then an optional legacy suffix such as ``_bn``."""
    if not locale:
        return None
    value = data.pop(f"{field}_{locale}", None)
    if value is not None:
        return value
    if fallback_suffix:
        return data.pop(f"{field}{fallback_suffix}", None)
    return None


def migrate_legacy_suffix_field(
    data: dict[str, Any],
    field: str,
    *,
    primary: str = "bn",
    legacy_mirror: str | None = "en",
) -> None:
    """Convert ``field_bn`` / ``field_en`` keys to ``field`` localized map in-place."""
    if field in data:
        return
    primary_val = _pop_legacy_locale_value(data, field, primary, fallback_suffix=_LEGACY_PRIMARY_SUFFIX)
    if primary_val is None:
        if legacy_mirror:
            primary_val = _pop_legacy_locale_value(
                data, field, legacy_mirror, fallback_suffix=_LEGACY_MIRROR_SUFFIX
            )
    if primary_val is None:
        return
    data[field] = build_localized_string(
        primary,
        primary_text=primary_val if isinstance(primary_val, str) else None,
    )


def migrate_legacy_suffix_list_field(
    data: dict[str, Any],
    field: str,
    *,
    primary: str = "bn",
    legacy_mirror: str | None = "en",
) -> None:
    """Convert ``field_bn`` / ``field_en`` list keys to ``field`` locale map in-place."""
    if field in data:
        return
    primary_val = _pop_legacy_locale_value(data, field, primary, fallback_suffix=_LEGACY_PRIMARY_SUFFIX)
    if primary_val is None and legacy_mirror:
        primary_val = _pop_legacy_locale_value(
            data, field, legacy_mirror, fallback_suffix=_LEGACY_MIRROR_SUFFIX
        )
    if primary_val is None:
        return
    data[field] = {primary: primary_val}


def migrate_legacy_card(
    card: dict[str, Any], *, primary: str = "bn", legacy_mirror: str | None = "en"
) -> dict[str, Any]:
    out = dict(card)
    for field in LOCALIZED_CARD_TEXT_FIELDS:
        migrate_legacy_suffix_field(out, field, primary=primary, legacy_mirror=legacy_mirror)
    sm = out.get("search_metadata")
    if isinstance(sm, dict):
        out["search_metadata"] = migrate_legacy_search_metadata(
            sm, primary=primary, legacy_mirror=legacy_mirror
        )
    return out


def migrate_legacy_search_metadata(
    metadata: dict[str, Any],
    *,
    primary: str = "bn",
    legacy_mirror: str | None = "en",
) -> dict[str, Any]:
    out = dict(metadata)
    for field in LOCALIZED_SEARCH_METADATA_LIST_FIELDS:
        migrate_legacy_suffix_list_field(out, field, primary=primary, legacy_mirror=legacy_mirror)
    return out


def migrate_legacy_module_json(
    module_json: dict[str, Any] | None,
    *,
    primary: str = "bn",
    legacy_mirror: str | None = "en",
) -> dict[str, Any] | None:
    if not module_json:
        return module_json
    out = dict(module_json)
    cards = out.get("cards")
    if isinstance(cards, list):
        out["cards"] = [
            migrate_legacy_card(c, primary=primary, legacy_mirror=legacy_mirror)
            for c in cards
            if isinstance(c, dict)
        ]
    return out


def to_localized_options(
    primary_options: list[Any] | None,
    *,
    settings: Settings | None = None,
) -> LocalizedOptions:
    primary = deployment_locales(settings)
    out: LocalizedOptions = {}
    if primary_options is not None:
        out[primary] = primary_options
    return out


def primary_options(
    localized: LocalizedOptions | None, *, settings: Settings | None = None
) -> list[Any] | None:
    primary = deployment_locales(settings)
    if not localized:
        return None
    return localized.get(primary)


def extract_localized_string_from_raw(
    raw: dict[str, Any],
    field: str,
    *,
    settings: Settings | None = None,
) -> LocalizedString:
    """Resolve a locale-keyed string field from a dict, including legacy suffix keys."""
    s = settings or get_settings()
    primary = deployment_locales(s)
    data = dict(raw)
    migrate_legacy_suffix_field(data, field, primary=primary)
    value = data.get(field)
    if isinstance(value, dict):
        return to_localized_string(primary_text(value, settings=s), settings=s)
    return to_localized_string(None, settings=s)


def extract_localized_options_from_raw(
    raw: dict[str, Any],
    *,
    field: str = "options",
    settings: Settings | None = None,
) -> LocalizedOptions:
    """Resolve locale-keyed quiz options from a dict, including legacy suffix keys."""
    s = settings or get_settings()
    primary = deployment_locales(s)
    data = dict(raw)
    migrate_legacy_suffix_list_field(data, field, primary=primary)
    value = data.get(field)
    if isinstance(value, dict):
        primary_opts = value.get(primary)
        return to_localized_options(primary_opts, settings=s)
    return to_localized_options(None, settings=s)


def localized_list_field_has_content(
    metadata: dict[str, Any],
    field: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """Return True when a locale-keyed list field has at least one non-empty list."""
    s = settings or get_settings()
    primary = deployment_locales(s)
    data = migrate_legacy_search_metadata(dict(metadata), primary=primary)
    value = data.get(field)
    return isinstance(value, dict) and any(value.values())
