"""Unit tests for strict markdown formatting stripping."""

from __future__ import annotations

from platform_service.services.llm_text_utils import strip_markdown_formatting


def test_strip_markdown_formatting_strips_headings_lists_quotes_and_fences() -> None:
    raw = "\n".join(
        [
            "## Heading",
            "",
            "- First",
            "1. Second",
            "",
            "> Quote **bold**",
            "",
            "```python",
            "print('hi')",
            "```",
        ]
    )
    assert strip_markdown_formatting(raw) == "Heading\n\nFirst\nSecond\n\nQuote bold\n\nprint('hi')"


def test_strip_markdown_formatting_converts_tables_to_cells() -> None:
    raw = "\n".join(
        [
            "| col1 | col2 |",
            "|------|------|",
            "| a | b |",
        ]
    )
    assert strip_markdown_formatting(raw) == "col1  col2\na  b"


def test_strip_markdown_formatting_image_line_to_alt_text() -> None:
    assert strip_markdown_formatting("![alt text](https://example.com/x.png)") == "alt text"
