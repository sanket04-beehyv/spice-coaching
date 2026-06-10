"""W-3 Stage B — markdown_outline_parser unit tests."""

from platform_service.workers.extractors.markdown_outline_parser import (
    extract_topic_terms,
    find_heading_hits,
    parse_outline,
)

# ── Heading detection ──────────────────────────────────────────────────


class TestHeadingDetection:
    def test_finds_h1_through_h6(self) -> None:
        page = "# H1\n\n## H2\n\n### H3\n\n#### H4\n\n##### H5\n\n###### H6"
        hits = find_heading_hits([(1, page)])
        assert [h.level for h in hits] == [1, 2, 3, 4, 5, 6]
        assert [h.text for h in hits] == ["H1", "H2", "H3", "H4", "H5", "H6"]

    def test_seven_or_more_hashes_not_a_heading(self) -> None:
        # Markdown spec: max 6 levels.
        hits = find_heading_hits([(1, "####### too deep")])
        assert hits == []

    def test_heading_requires_space_after_hashes(self) -> None:
        hits = find_heading_hits([(1, "##noSpace\n## with space")])
        assert len(hits) == 1
        assert hits[0].text == "with space"

    def test_ignores_headings_inside_fenced_code(self) -> None:
        page = "# Real heading\n\n```python\n## Not a heading\n```\n\n## Another real heading"
        hits = find_heading_hits([(1, page)])
        assert [h.text for h in hits] == ["Real heading", "Another real heading"]

    def test_handles_unclosed_code_fence(self) -> None:
        # Per markdown semantics, an unclosed fence runs to EOF — anything
        # after it is inside the fence.
        page = "# Heading\n```\n## NotAHeading"
        hits = find_heading_hits([(1, page)])
        assert [h.text for h in hits] == ["Heading"]

    def test_records_page_number(self) -> None:
        hits = find_heading_hits([(5, "## On page 5"), (7, "## On page 7")])
        assert [h.page_number for h in hits] == [5, 7]

    def test_heading_in_bangla(self) -> None:
        hits = find_heading_hits([(1, "# অধ্যায় ১\n\n## উপধারা")])
        assert [h.text for h in hits] == ["অধ্যায় ১", "উপধারা"]

    def test_skips_empty_pages(self) -> None:
        hits = find_heading_hits([(1, ""), (2, "# On page 2")])
        assert len(hits) == 1
        assert hits[0].page_number == 2

    def test_empty_heading_text_skipped(self) -> None:
        hits = find_heading_hits([(1, "# \n\n## Real")])
        assert [h.text for h in hits] == ["Real"]


# ── Topic-term extraction ──────────────────────────────────────────────


class TestTopicTermExtractionEnglish:
    def test_capitalised_phrases(self) -> None:
        text = "Antenatal Care is provided by Community Health Workers in rural settings."
        terms = extract_topic_terms(text, language="en")
        assert "Antenatal Care" in terms
        assert "Community Health Workers" in terms

    def test_skips_sentence_starts(self) -> None:
        text = "This is body text. The next sentence starts here."
        terms = extract_topic_terms(text, language="en")
        # "This" and "The" should be skipped as sentence starts.
        assert "This" not in terms
        assert "The" not in terms

    def test_caps_at_max_terms(self) -> None:
        # Distinct sentences so each capitalised phrase is a separate match
        # (the regex greedily groups runs of consecutive Caps Words).
        text = (
            "Antenatal Care matters. Postnatal Care matters too. "
            "Family Planning is important. Risk Management saves lives. "
            "Quality Assurance is critical. Data Entry is needed."
        )
        terms = extract_topic_terms(text, language="en", max_terms=3)
        assert len(terms) == 3

    def test_empty_text_returns_empty(self) -> None:
        assert extract_topic_terms("", language="en") == []
        assert extract_topic_terms("   ", language="en") == []


class TestTopicTermExtractionBangla:
    def test_extracts_unique_tokens(self) -> None:
        text = "এটি একটি বাংলা উদাহরণ পৃষ্ঠা যেখানে কিছু গুরুত্বপূর্ণ পরিভাষা রয়েছে।"
        terms = extract_topic_terms(text, language="bn", max_terms=5)
        assert len(terms) == 5
        assert all(len(t) >= 3 for t in terms)
        # Tokens should be unique
        assert len(terms) == len(set(terms))

    def test_caps_at_max_terms_bn(self) -> None:
        text = "এক দুই তিন চার পাঁচ ছয় সাত আট নয় দশ এগারো বারো"
        terms = extract_topic_terms(text, language="bn", max_terms=4)
        assert len(terms) == 4


# ── Tree construction ──────────────────────────────────────────────────


class TestSectionTree:
    def test_simple_two_section_doc(self) -> None:
        pages = [(1, "# Section A\n\nContent here.\n\n# Section B\n\nMore content.")]
        outline = parse_outline(pages, total_pages=1, primary_language="en")
        assert len(outline.sections) == 2
        assert outline.sections[0].heading == "Section A"
        assert outline.sections[1].heading == "Section B"
        assert outline.sections[0].section_id == "s1"
        assert outline.sections[1].section_id == "s2"

    def test_nested_tree(self) -> None:
        pages = [
            (
                1,
                "# Top\n\n## Sub 1\n\nP1.\n\n### SubSub 1\n\nP2.\n\n## Sub 2\n\nP3.",
            )
        ]
        outline = parse_outline(pages, total_pages=1, primary_language="en")
        assert len(outline.sections) == 1
        top = outline.sections[0]
        assert top.heading == "Top"
        assert len(top.subsections) == 2
        assert top.subsections[0].heading == "Sub 1"
        assert top.subsections[0].subsections[0].heading == "SubSub 1"
        assert top.subsections[0].subsections[0].section_id == "s1.1.1"
        assert top.subsections[1].heading == "Sub 2"
        assert top.subsections[1].section_id == "s1.2"

    def test_inconsistent_levels_jump_h1_to_h3(self) -> None:
        # When heading level jumps (h1 → h3), the parser preserves both at
        # their declared levels — h3 becomes a child of the h1.
        pages = [(1, "# Top\n\n### Skipped Level\n\nbody")]
        outline = parse_outline(pages, total_pages=1, primary_language="en")
        assert len(outline.sections) == 1
        top = outline.sections[0]
        assert top.level == 1
        assert len(top.subsections) == 1
        assert top.subsections[0].level == 3

    def test_page_range_covers_section_span(self) -> None:
        pages = [
            (1, "# A"),
            (2, "page 2 body"),
            (3, "page 3 body"),
            (4, "# B"),
            (5, "page 5 body"),
        ]
        outline = parse_outline(pages, total_pages=5, primary_language="en")
        assert len(outline.sections) == 2
        assert outline.sections[0].page_range == (1, 3)
        assert outline.sections[1].page_range == (4, 5)

    def test_page_range_ascends_to_total_pages_for_last_section(self) -> None:
        pages = [(1, "# only heading"), (2, ""), (3, "")]
        outline = parse_outline(pages, total_pages=3, primary_language="en")
        assert outline.sections[0].page_range == (1, 3)

    def test_no_headings_yields_empty_sections(self) -> None:
        outline = parse_outline(
            [(1, "Just body"), (2, "More body")],
            total_pages=2,
            primary_language="en",
        )
        assert outline.sections == []
        assert outline.heading_hits == []

    def test_single_page_document(self) -> None:
        outline = parse_outline(
            [(1, "# Solo\n\nBody.")],
            total_pages=1,
            primary_language="en",
        )
        assert len(outline.sections) == 1
        assert outline.sections[0].page_range == (1, 1)

    def test_topics_extracted_from_first_paragraph(self) -> None:
        pages = [
            (
                1,
                "# Antenatal Care\n\nAntenatal Care is provided by Community Health Workers.",
            )
        ]
        outline = parse_outline(pages, total_pages=1, primary_language="en")
        topics = outline.sections[0].topics
        assert "Antenatal Care" in topics or "Community Health Workers" in topics

    def test_inferred_title_is_first_h1(self) -> None:
        pages = [(1, "## sub first\n\n# Real Title\n\n## another sub")]
        outline = parse_outline(pages, total_pages=1, primary_language="en")
        assert outline.document_title_inferred == "Real Title"

    def test_no_h1_yields_null_title(self) -> None:
        pages = [(1, "## no h1 here")]
        outline = parse_outline(pages, total_pages=1, primary_language="en")
        assert outline.document_title_inferred is None


class TestParsedOutlineSerialisation:
    def test_to_jsonb_round_trip(self) -> None:
        pages = [(1, "# A\n\n## A.1"), (2, "# B")]
        outline = parse_outline(pages, total_pages=2, primary_language="en")
        out = outline.to_jsonb()
        assert out["primary_language"] == "en"
        assert out["document_title_inferred"] == "A"
        assert len(out["sections"]) == 2
        assert out["sections"][0]["section_id"] == "s1"
        assert out["sections"][0]["page_range"] == [1, 1]
        assert out["sections"][0]["subsections"][0]["section_id"] == "s1.1"
