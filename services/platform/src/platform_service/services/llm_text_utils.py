"""Shared text cleanup for LLM JSON and markdown responses."""

from __future__ import annotations

import re

_JSON_FENCE_START_RE = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_JSON_FENCE_END_RE = re.compile(r"\s*```$")
_CODE_FENCE_RE = re.compile(r"^```(?:\w+)?\s*\n?|\n?```$")
_LINE_FENCE_RE = re.compile(r"^```")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)")
_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!_)_([^_]+?)_(?!_)")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HEADING_PREFIX_RE = re.compile(r"^\s*#{1,6}\s+")
_BLOCKQUOTE_PREFIX_RE = re.compile(r"^\s*>\s?")
_LIST_ITEM_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_IMAGE_LINE_RE = re.compile(r"^\s*!\[(.*?)\]\((?:[^)]+)\)\s*$")
_TABLE_PIPE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*[:\-| ]+\s*\|?\s*$")


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


def strip_inline_markdown(text: str) -> str:
    """Remove inline markdown emphasis, code spans, and link syntax."""
    result = text
    previous = None
    while previous != result:
        previous = result
        result = _BOLD_RE.sub(r"\1", result)
        result = _ITALIC_RE.sub(r"\1", result)
        result = _BOLD_UNDERSCORE_RE.sub(r"\1", result)
        result = _ITALIC_UNDERSCORE_RE.sub(r"\1", result)
        result = _INLINE_CODE_RE.sub(r"\1", result)
        result = _LINK_RE.sub(r"\1", result)
    return result.strip()


def strip_markdown_formatting(text: str) -> str:
    """Strip markdown formatting (block + inline) while preserving readable text.

    This is stricter than `strip_inline_markdown`: it removes headings, list
    prefixes, blockquotes, code fences, basic table syntax, and image syntax.
    """
    if not text:
        return ""

    lines = text.splitlines()

    # Remove fenced code markers but keep the code text itself.
    in_fence = False
    cleaned_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        if is_fence_line(line.strip()):
            in_fence = not in_fence
            continue

        # Normalize common markdown line-level constructs (safe even inside fences).
        line = _HEADING_PREFIX_RE.sub("", line)
        line = _BLOCKQUOTE_PREFIX_RE.sub("", line)

        # Image-only lines -> alt text.
        m = _IMAGE_LINE_RE.match(line)
        if m:
            line = m.group(1).strip()

        cleaned_lines.append(line)

    # Strip list prefixes per line (but preserve line breaks), and convert
    # table rows in place. A field can contain prose, a table, and code in
    # one value, so table handling must not discard the non-table lines.
    stripped: list[str] = []
    for line in cleaned_lines:
        if _TABLE_PIPE_RE.match(line):
            if _TABLE_SEPARATOR_RE.match(line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            cells = [strip_inline_markdown(c) for c in cells if c]
            if cells:
                stripped.append("  ".join(cells))
            continue
        line = _LIST_ITEM_PREFIX_RE.sub("", line).strip()
        stripped.append(strip_inline_markdown(line) if line else "")

    # Keep newlines but drop leading/trailing blank lines.
    # Also collapse excessive blank lines to a maximum of one.
    out_lines: list[str] = []
    blank_run = 0
    for line in stripped:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                out_lines.append("")
            continue
        blank_run = 0
        out_lines.append(line.strip())

    return "\n".join(out_lines).strip()
