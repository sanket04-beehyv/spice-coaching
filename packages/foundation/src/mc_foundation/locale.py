"""Locale registry: script ranges, display names, and prompt metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

LocaleCode: TypeAlias = str

# Unicode codepoint ranges for native-script detection (extraction quality, bleed checks).
NATIVE_SCRIPT_RANGES: dict[str, tuple[int, int]] = {
    "bn": (0x0980, 0x09FF),
    "hi": (0x0900, 0x097F),
    "mr": (0x0900, 0x097F),
    "ta": (0x0B80, 0x0BFF),
    "te": (0x0C00, 0x0C7F),
    "bn_en_mixed": (0x0980, 0x09FF),
}


@dataclass(frozen=True, slots=True)
class LocaleMetadata:
    code: str
    display_name: str
    symbol_verbalization_examples: tuple[str, ...] = ()
    annexure_terms: tuple[str, ...] = ()
    vision_heading_examples: tuple[str, ...] = ()


LOCALE_REGISTRY: dict[str, LocaleMetadata] = {
    "bn": LocaleMetadata(
        code="bn",
        display_name="Bangla (bn)",
        symbol_verbalization_examples=(
            '`20-30` -> "20 থেকে 30" (range, not subtraction)',
            '`>=20` -> "20 বা তার বেশি"',
            '`<=20` -> "20 বা তার কম"',
        ),
        annexure_terms=("পরিশিষ্ট",),
        vision_heading_examples=("পাঠ শিরোনাম", "উদ্দেশ্য", "প্রক্রিয়া"),
    ),
    "hi": LocaleMetadata(
        code="hi",
        display_name="Hindi (hi)",
        symbol_verbalization_examples=(
            '`20-30` -> "20 से 30" (range, not subtraction)',
            '`>=20` -> "20 या अधिक"',
            '`<=20` -> "20 या उससे कम"',
        ),
        annexure_terms=("परिशिष्ट",),
        vision_heading_examples=("पाठ शीर्षक", "उद्देश्य", "प्रक्रिया"),
    ),
    "ta": LocaleMetadata(
        code="ta",
        display_name="Tamil (ta)",
        symbol_verbalization_examples=(
            '`20-30` -> "20 முதல் 30 வரை" (range, not subtraction)',
            '`>=20` -> "20 அல்லது அதற்கு மேல்"',
            '`<=20` -> "20 அல்லது அதற்குக் கீழ்"',
        ),
        annexure_terms=("இணைப்பு",),
        vision_heading_examples=("பாடத் தலைப்பு", "நோக்கம்", "செயல்முறை"),
    ),
    "te": LocaleMetadata(
        code="te",
        display_name="Telugu (te)",
        symbol_verbalization_examples=(
            '`20-30` -> "20 నుండి 30" (range, not subtraction)',
            '`>=20` -> "20 లేదా అంతకంటే ఎక్కువ"',
            '`<=20` -> "20 లేదా అంతకంటే తక్కువ"',
        ),
        annexure_terms=("అనుబంధం",),
        vision_heading_examples=("పాఠ శీర్షిక", "ఉద్దేశ్యం", "ప్రక్రియ"),
    ),
    "en": LocaleMetadata(
        code="en",
        display_name="English (en)",
        symbol_verbalization_examples=(
            '`20-30` -> "20 to 30" (range, not subtraction)',
            '`>=20` -> "20 or more"',
            '`<=20` -> "20 or less"',
        ),
        annexure_terms=("Annexure", "Appendix"),
        vision_heading_examples=("Lesson Title", "Objective", "Procedure"),
    ),
}


def get_locale_metadata(locale: str) -> LocaleMetadata:
    if locale in LOCALE_REGISTRY:
        return LOCALE_REGISTRY[locale]
    return LocaleMetadata(code=locale, display_name=locale)


def get_supported_locales(primary: str) -> frozenset[str]:
    return frozenset({primary})


def get_required_locales(primary: str) -> tuple[str, ...]:
    return (primary,)


def get_script_range(locale: str) -> tuple[int, int] | None:
    return NATIVE_SCRIPT_RANGES.get(locale)


def locale_display_name(locale: str) -> str:
    return get_locale_metadata(locale).display_name


def build_localized_string(
    primary: str,
    *,
    primary_text: str | None,
) -> dict[str, str]:
    """Build a locale-keyed map from the deployment primary locale value."""
    out: dict[str, str] = {}
    if primary_text and primary_text.strip():
        out[primary] = primary_text.strip()
    return out


def localized_primary_text(
    localized: dict[str, str] | None,
    primary: str,
) -> str | None:
    if not localized:
        return None
    value = localized.get(primary)
    return value.strip() if value else None


# Card / metadata field suffixes migrated from *_bn / *_en to localized maps.
LOCALIZED_CARD_TEXT_FIELDS: tuple[str, ...] = (
    "title",
    "body",
    "previous_practice",
    "current_practice",
    "rationale_for_change",
    "next_action",
)

LOCALIZED_SEARCH_METADATA_LIST_FIELDS: tuple[str, ...] = (
    "keywords",
    "search_phrases",
    "retrieval_hints",
    "questions",
    "topic_tags",
)
