"""Plain-text extraction for card body fields (legacy string or rich-text blocks)."""

from __future__ import annotations

import logging
from typing import Any

from platform_service.services.llm_text_utils import strip_inline_markdown

logger = logging.getLogger(__name__)

_BLOCK_NODE_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "blockquote",
        "codeBlock",
        "listItem",
        "bulletList",
        "orderedList",
        "table",
        "tableRow",
        "tableCell",
        "tableHeader",
        "horizontalRule",
    }
)


def is_prosemirror_doc(value: Any) -> bool:
    """True when ``value`` looks like a TipTap / ProseMirror root document."""
    return isinstance(value, dict) and value.get("type") == "doc"


def _is_block_node(value: Any) -> bool:
    """True when ``value`` looks like a single rich-text block node."""
    if not isinstance(value, dict):
        return False
    node_type = value.get("type")
    return isinstance(node_type, str) and bool(node_type) and node_type != "doc"


def is_rich_text_blocks(value: Any) -> bool:
    """True when ``value`` is a list of rich-text block nodes (may be empty)."""
    if not isinstance(value, list):
        return False
    return all(_is_block_node(item) for item in value)


def is_rich_text_body(value: Any) -> bool:
    """True when ``value`` is a ProseMirror doc, block list, or single block node."""
    return is_prosemirror_doc(value) or is_rich_text_blocks(value) or _is_block_node(value)


def card_body_plain_text(value: Any) -> str:
    """Return readable plain text from a localized body locale value."""
    if value is None:
        return ""
    if isinstance(value, str):
        return strip_inline_markdown(value)
    if is_prosemirror_doc(value):
        return _prosemirror_doc_to_plain_text(value).strip()
    if is_rich_text_blocks(value):
        return _rich_text_blocks_to_plain_text(value).strip()
    if isinstance(value, dict) and _is_block_node(value):
        return _node_plain_text(value, block_level=True).strip()
    if isinstance(value, dict):
        logger.warning("card body dict is not rich-text blocks; harvesting text best-effort")
        return _harvest_text_from_node(value).strip()
    return str(value).strip()


def card_body_is_nonempty(value: Any) -> bool:
    """True when the body has non-whitespace plain-text content."""
    return bool(card_body_plain_text(value))


def card_body_char_len(value: Any) -> int:
    """Character length of rendered plain text (not JSON wire size)."""
    return len(card_body_plain_text(value))


def _rich_text_blocks_to_plain_text(blocks: list[Any]) -> str:
    parts: list[str] = []
    for node in blocks:
        if not isinstance(node, dict):
            continue
        block_text = _node_plain_text(node, block_level=True).strip()
        if block_text:
            parts.append(block_text)
    return "\n".join(parts)


def _prosemirror_doc_to_plain_text(doc: dict[str, Any]) -> str:
    content = doc.get("content")
    if not isinstance(content, list):
        return ""
    return _rich_text_blocks_to_plain_text(content)


def _node_plain_text(node: dict[str, Any], *, block_level: bool = False) -> str:
    node_type = node.get("type")
    if node_type == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""
    if node_type == "hardBreak":
        return "\n"

    content = node.get("content")
    if not isinstance(content, list):
        return ""

    if node_type in ("bulletList", "orderedList"):
        items = [
            _node_plain_text(child, block_level=False).strip() for child in content if isinstance(child, dict)
        ]
        return "\n".join(item for item in items if item)

    if node_type == "listItem":
        inner = [
            _node_plain_text(child, block_level=False).strip() for child in content if isinstance(child, dict)
        ]
        return " ".join(part for part in inner if part)

    parts: list[str] = []
    for child in content:
        if not isinstance(child, dict):
            continue
        child_text = _node_plain_text(child, block_level=False)
        if child_text:
            parts.append(child_text)
    joined = "".join(parts) if node_type in ("paragraph", "heading") else " ".join(parts)
    if block_level and node_type in _BLOCK_NODE_TYPES:
        return joined.strip()
    return joined


def _harvest_text_from_node(node: Any) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""
    content = node.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(_harvest_text_from_node(child) for child in content)
