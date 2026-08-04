"""Unit tests for plain-text conversion of parsed content blocks."""

from platform_service.services.llm_text_utils import strip_inline_markdown
from platform_service.workers.extractors.extraction_plain_text import block_content_to_plain_text


class TestStripInlineMarkdown:
    def test_bold_and_italic(self) -> None:
        assert strip_inline_markdown("**bold** and *italic*") == "bold and italic"

    def test_bold_and_italic_underscore(self) -> None:
        assert strip_inline_markdown("__bold__ and _italic_") == "bold and italic"

    def test_code_span(self) -> None:
        assert strip_inline_markdown("use `code` here") == "use code here"

    def test_markdown_link(self) -> None:
        assert strip_inline_markdown("see [manual](https://example.com)") == "see manual"

    def test_bare_url_unchanged(self) -> None:
        assert strip_inline_markdown("visit https://example.com") == "visit https://example.com"


class TestBlockContentToPlainText:
    def test_paragraph_strips_inline_markdown(self) -> None:
        result = block_content_to_plain_text(
            block_type="paragraph",
            content_text="BP **≥** 140/90",
        )
        assert result == "BP ≥ 140/90"

    def test_heading_strips_inline_markdown(self) -> None:
        result = block_content_to_plain_text(
            block_type="heading",
            content_text="Risk **Factors**",
        )
        assert result == "Risk Factors"

    def test_list_strips_prefixes(self) -> None:
        raw = "- First\n- Second"
        result = block_content_to_plain_text(block_type="list", content_text=raw)
        assert result == "First\nSecond"

    def test_ordered_list_strips_prefixes(self) -> None:
        raw = "1. first\n2. second"
        result = block_content_to_plain_text(block_type="list", content_text=raw)
        assert result == "first\nsecond"

    def test_table_drops_pipes_and_separator(self) -> None:
        raw = "| col1 | col2 |\n|------|------|\n| a | b |"
        result = block_content_to_plain_text(block_type="table", content_text=raw)
        assert result == "col1  col2\na  b"

    def test_figure_extracts_alt_text(self) -> None:
        raw = "![alt text](path/to/image.png)"
        result = block_content_to_plain_text(block_type="figure", content_text=raw)
        assert result == "alt text"

    def test_callout_strips_inline_markdown(self) -> None:
        result = block_content_to_plain_text(
            block_type="callout",
            content_text="**Warning** here",
        )
        assert result == "Warning here"

    def test_empty_string(self) -> None:
        assert block_content_to_plain_text(block_type="paragraph", content_text="") == ""
