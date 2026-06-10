"""Truncation-tolerant JSON salvage helpers — shared by Stage 2 identifier.

Real LLM responses on long Stage 2 corpora regularly hit `max_output_tokens`
mid-stream. Standard `json.loads` then raises `JSONDecodeError` and the
whole stage fails — even when the response had several complete elements
before the cut. These helpers walk the partial output and return whichever
elements completed, so the run produces a degraded-but-non-empty result
that the dashboard can flag for reviewer attention.

Two output shapes are tolerated:
- Bare top-level array: `[{...}, {...}, {...truncated`
- Wrapped object:       `{"candidates": [{...}, {...}, {...truncated`
                        `{"consolidated_candidates": [{...}, {...truncated`

Public helpers:
- `salvage_truncated_array(text)` — recover complete elements from a
  raw bare-array response.
- `extract_inner_array(text, key)` — locate the start of `[` for a
  wrapped-object response. Pair with `salvage_truncated_array` on the
  result to recover from the truncated wrapper.
"""

from __future__ import annotations

import json
from typing import Any


def salvage_truncated_array(raw_text: str) -> list[dict[str, Any]] | None:
    """Recover whichever complete elements landed before a truncation cut.

    Walks the array tracking depth-1 closures (`}` at the array's top
    level), and on truncation rebuilds the array with whichever elements
    completed before the cut. Returns None when even the first element
    is incomplete, or when the whole array closed cleanly (in which case
    the caller should use `json.loads` directly).
    """
    text = raw_text.strip()
    if not text.startswith("["):
        return None
    depth = 0
    in_string = False
    escape = False
    last_complete_end = 0
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{" or ch == "[":
            depth += 1
        elif ch == "}" or ch == "]":
            depth -= 1
            if depth == 1 and ch == "}":
                last_complete_end = i + 1
            elif depth == 0:
                # Whole array closed — not actually truncated; let the
                # main parse path handle it.
                return None
    if last_complete_end == 0:
        return None
    candidate = text[:last_complete_end] + "]"
    try:
        result = json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, list) else None


def extract_inner_array(raw_text: str, *, key: str) -> str | None:
    """Locate the start of an array value inside a wrapped JSON object.

    Used as the first step of wrapper-aware salvage: the LLM emits
    `{"<key>": [...]}` and the wrapper plus the `[` survive truncation
    even when an inner element didn't. This finds the substring starting
    at the inner `[`, which `salvage_truncated_array` can then walk.

    Returns None when the marker (`"<key>"`) isn't in the response, or
    when no `[` follows the marker.
    """
    marker = f'"{key}"'
    idx = raw_text.find(marker)
    if idx == -1:
        return None
    array_start = raw_text.find("[", idx + len(marker))
    if array_start == -1:
        return None
    return raw_text[array_start:]


# Backwards-compat shims — kept until callers migrate to the public names.
_salvage_truncated_array = salvage_truncated_array
_extract_inner_array = extract_inner_array
