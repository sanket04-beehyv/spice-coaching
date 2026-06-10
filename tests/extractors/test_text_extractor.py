"""W-2 Stage A — text_extractor unit tests using programmatic fixtures."""

from pathlib import Path

import pytest
from platform_service.workers.extractors.text_extractor import (
    TextExtractionError,
    extract_pages,
    extract_pdf_pages,
)

from tests.extractors.fixture_builders import (
    build_clean_english_docx,
    build_clean_english_pdf,
    build_clean_english_pptx,
    build_corrupted_pdf,
    build_empty_pdf,
    build_image_only_pdf,
    build_unicode_bangla_pdf,
)

# ── PDF ────────────────────────────────────────────────────────────────


class TestPdfExtraction:
    def test_clean_english_three_pages(self, tmp_path: Path) -> None:
        pdf = build_clean_english_pdf(tmp_path / "clean.pdf", page_count=3)
        pages = extract_pdf_pages(pdf)
        assert len(pages) == 3
        for p in pages:
            assert p.markdown
            assert "Page" in p.markdown
        assert pages[0].page_number == 1
        assert pages[2].page_number == 3

    def test_unicode_bangla_extracts_bangla_codepoints(self, tmp_path: Path) -> None:
        pdf = build_unicode_bangla_pdf(tmp_path / "bn.pdf", page_count=2)
        pages = extract_pdf_pages(pdf)
        assert len(pages) == 2
        # Should contain Bangla codepoints
        all_text = "".join(p.markdown for p in pages)
        bangla_chars = sum(1 for ch in all_text if 0x0980 <= ord(ch) <= 0x09FF)
        assert bangla_chars > 0, f"no Bangla codepoints in extracted text: {all_text[:200]!r}"

    def test_image_only_pdf_returns_near_empty_text(self, tmp_path: Path) -> None:
        pdf = build_image_only_pdf(tmp_path / "img.pdf", page_count=2)
        pages = extract_pdf_pages(pdf)
        assert len(pages) == 2
        # No extractable text → empty/whitespace
        for p in pages:
            assert len(p.markdown.strip()) < 50

    def test_empty_pdf_returns_zero_pages(self, tmp_path: Path) -> None:
        pdf = build_empty_pdf(tmp_path / "empty.pdf")
        pages = extract_pdf_pages(pdf)
        assert pages == []

    def test_corrupted_pdf_raises(self, tmp_path: Path) -> None:
        pdf = build_corrupted_pdf(tmp_path / "bad.pdf")
        with pytest.raises(TextExtractionError):
            extract_pdf_pages(pdf)

    def test_missing_pdf_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TextExtractionError):
            extract_pdf_pages(tmp_path / "does_not_exist.pdf")

    def test_page_numbers_are_1_indexed(self, tmp_path: Path) -> None:
        pdf = build_clean_english_pdf(tmp_path / "n.pdf", page_count=5)
        pages = extract_pdf_pages(pdf)
        assert [p.page_number for p in pages] == [1, 2, 3, 4, 5]


# ── PPTX ───────────────────────────────────────────────────────────────


class TestPptxExtraction:
    def test_clean_english_three_slides(self, tmp_path: Path) -> None:
        pptx = build_clean_english_pptx(tmp_path / "u.pptx", slide_count=3)
        pages = extract_pages(pptx, "pptx")
        assert len(pages) == 3
        # Each slide markdown should contain title as # heading + body lines
        for p in pages:
            assert "# UHIS Slide" in p.markdown
            assert "BP reference" in p.markdown

    def test_pptx_titles_become_h1_headings(self, tmp_path: Path) -> None:
        pptx = build_clean_english_pptx(tmp_path / "h.pptx", slide_count=2)
        pages = extract_pages(pptx, "pptx")
        for p in pages:
            assert p.markdown.startswith("# ")


# ── DOCX ───────────────────────────────────────────────────────────────


class TestDocxExtraction:
    def test_clean_english_returns_one_page(self, tmp_path: Path) -> None:
        docx = build_clean_english_docx(tmp_path / "m.docx")
        pages = extract_pages(docx, "docx")
        assert len(pages) == 1
        assert pages[0].page_number == 1

    def test_heading_styles_become_markdown_headings(self, tmp_path: Path) -> None:
        docx = build_clean_english_docx(tmp_path / "h.docx")
        pages = extract_pages(docx, "docx")
        md = pages[0].markdown
        # Heading 1 → "# "
        assert "# BRAC SK Manual: Antenatal Care" in md
        # Heading 2 → "## "
        assert "## Risk Factors" in md
        assert "## Referral Decisions" in md


# ── Dispatcher ─────────────────────────────────────────────────────────


class TestDispatcher:
    def test_unsupported_source_type_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TextExtractionError):
            extract_pages(tmp_path / "x.txt", "txt")

    def test_dispatcher_routes_pdf(self, tmp_path: Path) -> None:
        pdf = build_clean_english_pdf(tmp_path / "d.pdf", page_count=1)
        pages = extract_pages(pdf, "pdf")
        assert len(pages) == 1
        assert pages[0].markdown
