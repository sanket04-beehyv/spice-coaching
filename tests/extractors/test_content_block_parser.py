"""W-4 — content_block_parser unit tests."""

from uuid import uuid4

from platform_service.workers.extractors.content_block_parser import (
    estimate_token_count,
    parse_page_blocks,
)


class TestHeadings:
    def test_emits_heading_block(self) -> None:
        blocks = parse_page_blocks("# Title\n\nBody.")
        types = [b.block_type for b in blocks]
        assert "heading" in types
        heading = next(b for b in blocks if b.block_type == "heading")
        assert heading.content_text == "Title"

    def test_heading_path_inheritance(self) -> None:
        md = "# A\n\nbody A.\n\n## B\n\nbody B.\n\n### C\n\nbody C."
        blocks = parse_page_blocks(md)
        # A paragraph "lives inside" the section it's under, so its heading_path
        # includes the immediate parent heading. body C is under A→B→C.
        body_c = next(b for b in blocks if b.block_type == "paragraph" and "body C" in b.content_text)
        assert body_c.heading_path == ["A", "B", "C"]
        # The C heading block itself has path = its ancestors only (excludes self).
        c_heading = next(b for b in blocks if b.block_type == "heading" and b.content_text == "C")
        assert c_heading.heading_path == ["A", "B"]


class TestParagraphs:
    def test_paragraphs_separated_by_blank_lines(self) -> None:
        md = "Para one.\n\nPara two."
        blocks = parse_page_blocks(md)
        para_texts = [b.content_text for b in blocks if b.block_type == "paragraph"]
        assert "Para one." in para_texts
        assert "Para two." in para_texts


class TestLists:
    def test_unordered_list(self) -> None:
        md = "- one\n- two\n- three"
        blocks = parse_page_blocks(md)
        list_blocks = [b for b in blocks if b.block_type == "list"]
        assert len(list_blocks) == 1
        assert "one" in list_blocks[0].content_text

    def test_ordered_list(self) -> None:
        md = "1. first\n2. second\n3. third"
        blocks = parse_page_blocks(md)
        list_blocks = [b for b in blocks if b.block_type == "list"]
        assert len(list_blocks) == 1


class TestTables:
    def test_emits_table_block(self) -> None:
        md = "| col1 | col2 |\n|------|------|\n| a    | b    |"
        blocks = parse_page_blocks(md)
        tables = [b for b in blocks if b.block_type == "table"]
        assert len(tables) == 1
        assert "| col1" in tables[0].content_text


class TestFigures:
    def test_image_line_emits_figure(self) -> None:
        md = "![alt text](path/to/image.png)"
        blocks = parse_page_blocks(md)
        figures = [b for b in blocks if b.block_type == "figure"]
        assert len(figures) == 1


class TestCallouts:
    def test_blockquote_emits_callout(self) -> None:
        md = "> warning here\n> on multiple lines"
        blocks = parse_page_blocks(md)
        callouts = [b for b in blocks if b.block_type == "callout"]
        assert len(callouts) == 1
        # Leading "> " stripped from stored content
        assert "warning here" in callouts[0].content_text
        assert ">" not in callouts[0].content_text


class TestCodeFences:
    def test_code_fence_content_treated_as_paragraph(self) -> None:
        md = "```python\nprint('hi')\n```"
        blocks = parse_page_blocks(md)
        # The fence itself is consumed; the content is captured as a paragraph
        paragraphs = [b for b in blocks if b.block_type == "paragraph"]
        assert len(paragraphs) == 1
        assert "print" in paragraphs[0].content_text

    def test_heading_inside_fence_not_emitted_as_heading(self) -> None:
        md = "# Real H\n\n```\n## Not a heading\n```"
        blocks = parse_page_blocks(md)
        headings = [b for b in blocks if b.block_type == "heading"]
        assert [h.content_text for h in headings] == ["Real H"]


class TestEmptyAndEdgeCases:
    def test_empty_string_returns_no_blocks(self) -> None:
        assert parse_page_blocks("") == []

    def test_whitespace_only_returns_no_blocks(self) -> None:
        assert parse_page_blocks("\n\n\n   \n") == []

    def test_block_order_increments_per_block(self) -> None:
        md = "# A\n\nbody.\n\n- item"
        blocks = parse_page_blocks(md)
        assert [b.block_order for b in blocks] == list(range(len(blocks)))

    def test_to_repo_dict_shape(self) -> None:
        blocks = parse_page_blocks("# A")
        d = blocks[0].to_repo_dict(source_page_id=uuid4(), content_language="en")
        assert d["block_type"] == "heading"
        assert d["content_language"] == "en"
        assert "heading_path_jsonb" in d


class TestTokenEstimate:
    def test_zero_for_empty(self) -> None:
        assert estimate_token_count("") == 0

    def test_minimum_one(self) -> None:
        assert estimate_token_count("x") >= 1

    def test_grows_with_length(self) -> None:
        a = estimate_token_count("hi")
        b = estimate_token_count("hello world this is longer")
        assert b > a
