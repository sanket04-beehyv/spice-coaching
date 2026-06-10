"""W-4 — markdown-to-ContentBlock parser.

Per Data Model v3.3 §3.3. Splits a page's markdown_content into semantic
content_block rows: heading | paragraph | list | table | figure | callout.

These become the citation primitives downstream: Stage C module candidates
cite content_block_ids; Stage D card drafting reads cited blocks for
verbatim source quotation.

The parser is deterministic (no LLM). It walks the markdown, respects
fenced code blocks, and inherits heading_path_jsonb from preceding
headings so each block knows where it sits in the document hierarchy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from platform_service.services.llm_text_utils import is_fence_line
from platform_service.services.token_estimation import estimate_token_count

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
_TABLE_PIPE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*[:\-| ]+\s*\|?\s*$")
# Figure: image syntax `![alt](src)` or HTML <img>; for our purposes, we
# treat any line that's solely an image as a figure.
_IMAGE_LINE_RE = re.compile(r"^\s*!\[.*?\]\(.+?\)\s*$")
# Callout: blockquotes (>) or GFM admonitions like `> [!NOTE]`
_BLOCKQUOTE_RE = re.compile(r"^\s*>")


@dataclass(frozen=True)
class ParsedBlock:
    """A semantic block extracted from a page's markdown."""

    block_order: int
    block_type: str  # heading | paragraph | list | table | figure | callout
    content_text: str
    heading_path: list[str]  # inherited section path at this block

    def to_repo_dict(self, *, source_page_id: Any, content_language: str | None) -> dict[str, Any]:
        """Shape compatible with SourceRepository.bulk_create_content_blocks."""
        return {
            "source_page_id": source_page_id,
            "block_order": self.block_order,
            "block_type": self.block_type,
            "content_text": self.content_text,
            "content_language": content_language,
            "heading_path_jsonb": list(self.heading_path),
        }


def _is_table_line(line: str) -> bool:
    return bool(_TABLE_PIPE_RE.match(line))


def _is_table_separator(line: str) -> bool:
    return bool(_TABLE_SEPARATOR_RE.match(line))


def parse_page_blocks(markdown: str) -> list[ParsedBlock]:
    """Walk a page's markdown and emit ContentBlock rows.

    Tracks heading_path_jsonb so a block under "# A → ## B" gets
    `["A", "B"]` as its inherited path.
    """
    if not markdown:
        return []

    blocks: list[ParsedBlock] = []
    heading_stack: list[tuple[int, str]] = []  # (level, text)
    in_fence = False
    pending_para: list[str] = []
    pending_list: list[str] = []
    pending_table: list[str] = []
    pending_callout: list[str] = []

    def _flush_paragraph() -> None:
        nonlocal pending_para
        if pending_para:
            blocks.append(
                ParsedBlock(
                    block_order=len(blocks),
                    block_type="paragraph",
                    content_text=" ".join(pending_para).strip(),
                    heading_path=[t for _, t in heading_stack],
                )
            )
            pending_para = []

    def _flush_list() -> None:
        nonlocal pending_list
        if pending_list:
            blocks.append(
                ParsedBlock(
                    block_order=len(blocks),
                    block_type="list",
                    content_text="\n".join(pending_list).strip(),
                    heading_path=[t for _, t in heading_stack],
                )
            )
            pending_list = []

    def _flush_table() -> None:
        nonlocal pending_table
        if pending_table:
            blocks.append(
                ParsedBlock(
                    block_order=len(blocks),
                    block_type="table",
                    content_text="\n".join(pending_table).strip(),
                    heading_path=[t for _, t in heading_stack],
                )
            )
            pending_table = []

    def _flush_callout() -> None:
        nonlocal pending_callout
        if pending_callout:
            # Strip leading "> " from each line for storage
            stripped = [re.sub(r"^\s*>\s?", "", line) for line in pending_callout]
            blocks.append(
                ParsedBlock(
                    block_order=len(blocks),
                    block_type="callout",
                    content_text="\n".join(stripped).strip(),
                    heading_path=[t for _, t in heading_stack],
                )
            )
            pending_callout = []

    def _flush_all() -> None:
        _flush_paragraph()
        _flush_list()
        _flush_table()
        _flush_callout()

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()

        # Fenced code blocks: included as a single "paragraph" block; flush
        # other accumulators first.
        if is_fence_line(line):
            in_fence = not in_fence
            continue
        if in_fence:
            # Inside a code fence, accumulate into a paragraph; we don't
            # honour markdown semantics here.
            pending_para.append(line)
            continue

        # Heading: flush all accumulators, push onto heading stack, emit
        # heading block.
        m = _HEADING_RE.match(line)
        if m:
            _flush_all()
            level = len(m.group(1))
            text = m.group(2).strip()
            # Pop heading_stack to depth < this level
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, text))
            blocks.append(
                ParsedBlock(
                    block_order=len(blocks),
                    block_type="heading",
                    content_text=text,
                    heading_path=[t for _, t in heading_stack[:-1]],  # exclude self
                )
            )
            continue

        # Blank line: paragraph break; flush text accumulators.
        if not line.strip():
            _flush_paragraph()
            _flush_list()
            _flush_table()
            _flush_callout()
            continue

        # Image line → figure
        if _IMAGE_LINE_RE.match(line):
            _flush_all()
            blocks.append(
                ParsedBlock(
                    block_order=len(blocks),
                    block_type="figure",
                    content_text=line.strip(),
                    heading_path=[t for _, t in heading_stack],
                )
            )
            continue

        # Blockquote / callout
        if _BLOCKQUOTE_RE.match(line):
            _flush_paragraph()
            _flush_list()
            _flush_table()
            pending_callout.append(line)
            continue

        # Table line
        if _is_table_line(line):
            _flush_paragraph()
            _flush_list()
            _flush_callout()
            # Skip table separator rows like |---|---| but still keep them
            # in the table body for fidelity.
            pending_table.append(line)
            continue

        # List item
        if _LIST_ITEM_RE.match(line):
            _flush_paragraph()
            _flush_table()
            _flush_callout()
            pending_list.append(line)
            continue

        # Plain paragraph line
        _flush_list()
        _flush_table()
        _flush_callout()
        pending_para.append(line.strip())

    _flush_all()
    return blocks


__all__ = ["ParsedBlock", "parse_page_blocks", "estimate_token_count"]
