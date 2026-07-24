"""Unit tests for Stage D cited block sanitization."""

from __future__ import annotations

from platform_service.services.draft_pipeline import _sanitize_cited_block_text


def test_sanitize_cited_block_text_preserves_markdown_for_legacy_blocks() -> None:
    raw = "- First\n- Second\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    assert _sanitize_cited_block_text(block_type="paragraph", content_text=raw) == raw
