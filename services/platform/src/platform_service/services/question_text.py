"""Shared helpers for free-text chatbot question strings."""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """Trim, collapse whitespace, and casefold for dedupe keys.

    Must stay aligned with the ClickHouse expression used by team-activity
    member questions: ``lowerUTF8(replaceRegexpAll(trimBoth(...), '\\\\s+', ' '))``.
    """
    return _WHITESPACE_RE.sub(" ", text.strip()).casefold()
