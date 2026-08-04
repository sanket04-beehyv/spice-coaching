"""Plain-text conversion utilities (service layer).

Stage C and Stage D want *readable text* without markdown formatting. Older
ingests may have markdown-y `content_block.content_text` in the DB, so Stage D
must defensively sanitize before passing blocks into the card drafter prompt.
"""

from __future__ import annotations

import re

from platform_service.services.llm_text_utils import strip_inline_markdown, strip_markdown_formatting

_IMAGE_LINE_RE = re.compile(r"^\s*!\[(.*?)\]\(.+?\)\s*$")
_LIST_ITEM_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_TABLE_PIPE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*[:\-| ]+\s*\|?\s*$")
_BLOCKQUOTE_PREFIX_RE = re.compile(r"^\s*>\s?")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")


def block_content_to_plain_text(*, block_type: str, content_text: str) -> str:
    """Strip markdown-ish syntax while preserving readable prose."""
    if not content_text:
        return ""

    # Normalize line-level markdown prefixes (safe for any block type).
    normalized_lines: list[str] = []
    for raw_line in content_text.splitlines():
        line = raw_line.rstrip()
        # Strip headings and blockquotes when they appear in stored content.
        line = _HEADING_RE.sub("", line)
        line = _BLOCKQUOTE_PREFIX_RE.sub("", line)
        normalized_lines.append(line)
    normalized = "\n".join(normalized_lines).strip()

    if block_type == "list":
        return _plain_list(normalized)
    if block_type == "table":
        return _plain_table(normalized)
    if block_type == "figure":
        return _plain_figure(normalized)

    return strip_markdown_formatting(normalized)


def _plain_list(content_text: str) -> str:
    lines: list[str] = []
    for line in content_text.splitlines():
        stripped = _LIST_ITEM_PREFIX_RE.sub("", line).strip()
        if stripped:
            lines.append(strip_inline_markdown(stripped))
    return "\n".join(lines)


def _plain_table(content_text: str) -> str:
    rows: list[str] = []
    for line in content_text.splitlines():
        if not _TABLE_PIPE_RE.match(line):
            continue
        if _TABLE_SEPARATOR_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        cells = [strip_inline_markdown(cell) for cell in cells if cell]
        if cells:
            rows.append("  ".join(cells))
    return "\n".join(rows)


def _plain_figure(content_text: str) -> str:
    match = _IMAGE_LINE_RE.match(content_text)
    if match:
        return match.group(1).strip()
    return strip_inline_markdown(content_text)


__all__ = ["block_content_to_plain_text"]
