"""Response parser — extracts structured JSON from raw LLM text.

Handles Gemini's tendency to wrap JSON in markdown fences or add preamble.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Strip markdown code fences: ```json ... ``` or ``` ... ```
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def extract_json(raw_text: str) -> dict | list | None:
    """Attempt to extract a JSON object or array from raw LLM output.

    Tries in order:
    1. Direct JSON parse of stripped text.
    2. Extract content from markdown code fences.
    3. Find the first { ... } or [ ... ] block.

    `strict=False` is passed everywhere so a stray bare control character
    inside a string (Gemini occasionally drops a literal \\n mid-string when
    asked for long structured output) doesn't fail the whole parse — without
    this, fallback #3 silently returns only the first nested object, which
    on Stage 2 looks like "1 candidate instead of N".
    """
    stripped = raw_text.strip()

    # 1. Direct parse
    try:
        return json.loads(stripped, strict=False)
    except json.JSONDecodeError:
        pass

    # 2. Markdown fence extraction
    m = _FENCE_RE.search(stripped)
    if m:
        try:
            return json.loads(m.group(1).strip(), strict=False)
        except json.JSONDecodeError:
            pass

    # 3. Find first JSON structure. Prefer `[` over `{` so list-shaped
    # responses with a malformed inner object don't get clipped to a
    # single object. If a top-level `[` is present but never closes
    # (truncation), try the salvage path BEFORE falling back to `{`,
    # otherwise we'd silently return only the first nested object.
    arr_start = stripped.find("[")
    obj_start = stripped.find("{")
    if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
        # Top-level array intended.
        complete = _try_balanced(stripped, arr_start, "[", "]")
        if complete is not None:
            return complete
        salvaged = _salvage_truncated_array(stripped[arr_start:])
        if salvaged is not None:
            logger.warning("Salvaged truncated JSON array (recovered %d items)", len(salvaged))
            return salvaged
    if obj_start != -1:
        complete = _try_balanced(stripped, obj_start, "{", "}")
        if complete is not None:
            return complete

    logger.warning("Failed to extract JSON from raw text (len=%d)", len(raw_text))
    return None


def _try_balanced(text: str, start: int, start_char: str, end_char: str) -> dict | list | None:
    """Walk balanced brackets from `start` and parse the matched span."""
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == start_char:
            depth += 1
        elif ch == end_char:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1], strict=False)
                except json.JSONDecodeError:
                    return None
    return None


def _salvage_truncated_array(text: str) -> list | None:
    """Best-effort recovery of a truncated top-level JSON array.

    Walks the text, tracking the position immediately after each top-level
    array element (i.e. each balanced `{...}` at depth 1). When parsing
    fails, returns the elements completed before the failure point. Returns
    None if even the first element couldn't be parsed.
    """
    if not text.startswith("["):
        return None
    depth = 0
    in_string = False
    escape = False
    last_complete_end = 0  # index just past the last complete element + comma
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
                # Just closed a top-level element. Mark this position.
                last_complete_end = i + 1
            elif depth == 0:
                # The whole array closed — let the normal path handle it.
                return None
    if last_complete_end == 0:
        return None
    candidate = text[:last_complete_end] + "]"
    try:
        result = json.loads(candidate, strict=False)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, list) else None
