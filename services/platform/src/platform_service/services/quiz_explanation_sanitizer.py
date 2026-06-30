"""Strip card-number citations from quiz explanation prose.

Quiz explanations are shown to CHWs after answering; they should explain
*why* an answer is correct without referencing internal card indices.
"""

from __future__ import annotations

import re
from typing import Any

_CARD_DIGITS = r"[0-9০-৯]+"

_ENGLISH_CITATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"(?i)\b(?:see|refer to)\s+card\s+{_CARD_DIGITS}(?:\s+for\s+details)?\b\.?"),
    re.compile(
        rf"(?i)\b(?:as (?:shown|stated|described) in|according to|based on|from|per|in)\s+card\s+{_CARD_DIGITS}\b,?\s*"
    ),
    re.compile(rf"(?i)\bcard\s+{_CARD_DIGITS}\s*(?:states?|shows?|explains?|mentions?|says?)\s+"),
    re.compile(rf"(?i)\(\s*card\s+{_CARD_DIGITS}\s*\)"),
    re.compile(rf"(?i)\bcard\s+{_CARD_DIGITS}\b"),
)

_BENGALI_CITATION_PATTERN = re.compile(rf",?\s*কার্ড\s*{_CARD_DIGITS}(?:\s*অনুযায়ী|\s*এ|\s*তে|-এ)?")

_MULTI_SPACE_RE = re.compile(r" +")
_EMPTY_PARENS_RE = re.compile(r"\(\s*\)")
_DANGLING_PUNCT_BEFORE_PERIOD_RE = re.compile(r"[,;—–-]+\s*\.")
_DANGLING_PUNCT_AT_END_RE = re.compile(r"[,;—–-]+\s*$")


def strip_card_citations_from_explanation(text: str) -> str:
    """Remove card-index citations from one explanation string."""
    if not text:
        return text

    result = text
    for pattern in _ENGLISH_CITATION_PATTERNS:
        result = pattern.sub("", result)
    result = _BENGALI_CITATION_PATTERN.sub("", result)
    return _normalize_explanation_text(result)


def sanitize_explanation_localized(
    explanation: dict[str, str] | None,
) -> dict[str, str] | None:
    """Strip card citations from every locale value in an explanation map."""
    if not explanation:
        return None
    sanitized = {
        locale: strip_card_citations_from_explanation(text)
        for locale, text in explanation.items()
        if isinstance(locale, str) and isinstance(text, str) and text
    }
    return sanitized or None


def sanitize_explanation_localized_value(value: Any) -> dict[str, str] | None:
    """Sanitize a JSONB explanation_localized value from the database."""
    if not isinstance(value, dict):
        return None
    return sanitize_explanation_localized(
        {locale: text for locale, text in value.items() if isinstance(locale, str) and isinstance(text, str)}
    )


def _normalize_explanation_text(text: str) -> str:
    cleaned = _MULTI_SPACE_RE.sub(" ", text)
    cleaned = _EMPTY_PARENS_RE.sub("", cleaned)
    cleaned = re.sub(r"^\s*,\s*", "", cleaned)
    cleaned = re.sub(r",\s*([।.])", r"\1", cleaned)
    cleaned = re.sub(r"\s+\.", ".", cleaned)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    cleaned = re.sub(r",\s*,", ",", cleaned)
    cleaned = _DANGLING_PUNCT_BEFORE_PERIOD_RE.sub(".", cleaned)
    cleaned = _DANGLING_PUNCT_AT_END_RE.sub("", cleaned)
    return cleaned.strip()
