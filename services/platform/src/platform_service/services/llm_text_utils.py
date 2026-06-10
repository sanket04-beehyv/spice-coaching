"""Shared text cleanup for LLM JSON and markdown responses."""

from __future__ import annotations

import re

_JSON_FENCE_START_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_JSON_FENCE_END_RE = re.compile(r"\s*```$")
_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?|\n?```$")
_LINE_FENCE_RE = re.compile(r"^```")


def strip_json_fence(raw: str) -> str:
    """Remove wrapping markdown JSON fences from a model response."""
    s = raw.strip()
    if s.startswith("```"):
        s = _JSON_FENCE_START_RE.sub("", s)
        s = _JSON_FENCE_END_RE.sub("", s)
    return s.strip()


def strip_code_fence(raw: str) -> str:
    """Strip start/end code fences from a full markdown body."""
    return _CODE_FENCE_RE.sub("", raw.strip()).strip()


def is_fence_line(line: str) -> bool:
    """True when a markdown line opens or closes a code fence."""
    return bool(_LINE_FENCE_RE.match(line))
