"""Unit tests for extraction markdown normalization."""

from platform_service.workers.extractors.extraction_markdown import normalize_extraction_markdown


class TestNormalizeExtractionMarkdown:
    def test_plain_markdown_unchanged(self) -> None:
        raw = "# Heading\n\n**bold** and - bullet"
        assert normalize_extraction_markdown(raw) == raw

    def test_empty_string(self) -> None:
        assert normalize_extraction_markdown("") == ""

    def test_bold_tag_to_markdown(self) -> None:
        assert normalize_extraction_markdown("<b>bold</b> text") == "**bold** text"

    def test_strong_tag_to_markdown(self) -> None:
        assert normalize_extraction_markdown("<strong>important</strong>") == "**important**"

    def test_italic_tags_to_markdown(self) -> None:
        assert normalize_extraction_markdown("<i>emphasis</i> and <em>more</em>") == "*emphasis* and *more*"

    def test_br_becomes_newline(self) -> None:
        assert normalize_extraction_markdown("line one<br>line two") == "line one\nline two"

    def test_unordered_list_to_markdown(self) -> None:
        raw = "<ul><li>First</li><li>Second</li></ul>"
        assert normalize_extraction_markdown(raw) == "- First\n- Second"

    def test_ordered_list_to_markdown(self) -> None:
        raw = "<ol><li>Step one</li><li>Step two</li></ol>"
        assert normalize_extraction_markdown(raw) == "1. Step one\n2. Step two"

    def test_strips_paragraph_tags(self) -> None:
        raw = "<p>Paragraph one</p><p>Paragraph two</p>"
        assert normalize_extraction_markdown(raw) == "Paragraph one\nParagraph two"

    def test_nested_bold_inside_list_item(self) -> None:
        raw = "<ul><li><b>Risk</b> category</li></ul>"
        assert normalize_extraction_markdown(raw) == "- **Risk** category"

    def test_collapses_excess_blank_lines(self) -> None:
        raw = "one\n\n\n\ntwo"
        assert normalize_extraction_markdown(raw) == "one\n\ntwo"

    def test_no_angle_brackets_remain(self) -> None:
        raw = "<div><b>Title</b><br/><ul><li>Item</li></ul></div>"
        result = normalize_extraction_markdown(raw)
        assert "<" not in result
        assert "**Title**" in result
        assert "- Item" in result

    def test_anchor_tag_keeps_link_text(self) -> None:
        raw = '<a href="https://example.com">Manual</a>'
        assert normalize_extraction_markdown(raw) == "Manual"

    def test_span_tag_unwrapped(self) -> None:
        raw = '<span style="color:red">Alert</span>'
        assert normalize_extraction_markdown(raw) == "Alert"

    def test_table_to_markdown_rows(self) -> None:
        raw = "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
        result = normalize_extraction_markdown(raw)
        assert "| a | b |" in result
        assert "| c | d |" in result

    def test_strips_control_characters(self) -> None:
        raw = "hello\x00world"
        assert normalize_extraction_markdown(raw) == "helloworld"
