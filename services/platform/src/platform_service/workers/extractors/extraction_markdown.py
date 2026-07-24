"""Normalize Stage A extraction output: HTML → markdown, strip residual tags."""

from __future__ import annotations

import html
import re
import unicodedata

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_OL_BLOCK_RE = re.compile(r"<ol(?:\s[^>]*)?>(.*?)</ol>", re.IGNORECASE | re.DOTALL)
_UL_BLOCK_RE = re.compile(r"<ul(?:\s[^>]*)?>(.*?)</ul>", re.IGNORECASE | re.DOTALL)
_LI_RE = re.compile(r"<li(?:\s[^>]*)?>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_BOLD_RE = re.compile(r"<(?:b|strong)(?:\s[^>]*)?>(.*?)</(?:b|strong)>", re.IGNORECASE | re.DOTALL)
_ITALIC_RE = re.compile(r"<(?:i|em)(?:\s[^>]*)?>(.*?)</(?:i|em)>", re.IGNORECASE | re.DOTALL)
_TABLE_BLOCK_RE = re.compile(r"<table(?:\s[^>]*)?>(.*?)</table>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr(?:\s[^>]*)?>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<t[dh](?:\s[^>]*)?>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(
    r"<a(?:\s[^>]*)?href\s*=\s*['\"][^'\"]*['\"](?:\s[^>]*)?>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_UNWRAP_RE = re.compile(
    r"<(?:span|font)(?:\s[^>]*)?>(.*?)</(?:span|font)>",
    re.IGNORECASE | re.DOTALL,
)
_BLOCK_END_RE = re.compile(r"</(?:p|div|tr|h[1-6])(?:\s[^>]*)?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def persist_markdown_content(raw: str) -> str:
    """Normalize extraction output before persisting to ``source_page.markdown_content``."""
    return normalize_extraction_markdown(raw)


def normalize_extraction_markdown(raw: str) -> str:
    """Convert HTML markup in extracted text to markdown; strip unknown tags."""
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", raw).strip()
    s = _CONTROL_CHAR_RE.sub("", s)
    if "<" not in s:
        return _collapse_blank_lines(s)

    s = _BR_RE.sub("\n", s)
    s = _convert_tables(s)
    s = _convert_lists(s)
    s = _convert_inline_emphasis(s)
    s = _unwrap_links_and_spans(s)
    s = _strip_remaining_tags(s)
    return _collapse_blank_lines(s)


def _convert_tables(text: str) -> str:
    return _TABLE_BLOCK_RE.sub(lambda m: _table_to_markdown(m.group(1)), text)


def _table_to_markdown(block: str) -> str:
    rows: list[str] = []
    for row_match in _TR_RE.finditer(block):
        cells = [cell.strip() for cell in _TD_RE.findall(row_match.group(1))]
        if not cells:
            continue
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def _unwrap_links_and_spans(text: str) -> str:
    result = text
    previous = None
    while previous != result:
        previous = result
        result = _ANCHOR_RE.sub(lambda m: m.group(1).strip(), result)
        result = _UNWRAP_RE.sub(lambda m: m.group(1).strip(), result)
    return result


def _convert_lists(text: str) -> str:
    result = _OL_BLOCK_RE.sub(lambda m: _list_items_to_markdown(m.group(1), ordered=True), text)
    result = _UL_BLOCK_RE.sub(lambda m: _list_items_to_markdown(m.group(1), ordered=False), result)
    return _LI_RE.sub(lambda m: f"- {m.group(1).strip()}", result)


def _list_items_to_markdown(block: str, *, ordered: bool) -> str:
    items = _LI_RE.findall(block)
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        cleaned = item.strip()
        if not cleaned:
            continue
        prefix = f"{index}. " if ordered else "- "
        lines.append(f"{prefix}{cleaned}")
    return "\n".join(lines)


def _convert_inline_emphasis(text: str) -> str:
    result = text
    previous = None
    while previous != result:
        previous = result
        result = _BOLD_RE.sub(lambda m: f"**{m.group(1).strip()}**", result)
        result = _ITALIC_RE.sub(lambda m: f"*{m.group(1).strip()}*", result)
    return result


def _strip_remaining_tags(text: str) -> str:
    result = _BLOCK_END_RE.sub("\n", text)
    result = _TAG_RE.sub("", result)
    return html.unescape(result)


def _collapse_blank_lines(text: str) -> str:
    collapsed = _EXCESS_BLANK_LINES_RE.sub("\n\n", text)
    return collapsed.strip()


__all__ = ["normalize_extraction_markdown", "persist_markdown_content"]
