"""Unit tests for Stage D cited block sanitization."""

from __future__ import annotations

from platform_service.services.draft_pipeline import _sanitize_cited_block_text


def test_sanitize_cited_block_text_strips_markdown_from_legacy_blocks() -> None:
    raw = "- First\n- Second\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert (
        _sanitize_cited_block_text(block_type="paragraph", content_text=raw) == "First\nSecond\n\na  b\n1  2"
    )
