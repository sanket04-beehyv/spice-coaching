"""W-2 Stage A — page_renderer unit tests."""

from pathlib import Path

import pytest
from platform_service.workers.extractors.page_renderer import (
    UnsupportedRenderError,
    count_pages,
    count_pdf_pages,
    render_page_to_png,
    render_pdf_page_to_png,
)

from tests.extractors.fixture_builders import (
    build_clean_english_pdf,
    build_clean_english_pptx,
    build_empty_pdf,
)


class TestPdfRendering:
    def test_renders_first_page_to_png_bytes(self, tmp_path: Path) -> None:
        pdf = build_clean_english_pdf(tmp_path / "r.pdf", page_count=2)
        png = render_pdf_page_to_png(pdf, page_number=1)
        assert png.startswith(b"\x89PNG"), "rendered output is not PNG"
        assert len(png) > 1000, "PNG suspiciously small"

    def test_renders_last_page(self, tmp_path: Path) -> None:
        pdf = build_clean_english_pdf(tmp_path / "r.pdf", page_count=3)
        png = render_pdf_page_to_png(pdf, page_number=3)
        assert png.startswith(b"\x89PNG")

    def test_out_of_range_page_raises(self, tmp_path: Path) -> None:
        pdf = build_clean_english_pdf(tmp_path / "r.pdf", page_count=2)
        with pytest.raises(IndexError):
            render_pdf_page_to_png(pdf, page_number=99)

    def test_empty_pdf_count_is_zero(self, tmp_path: Path) -> None:
        pdf = build_empty_pdf(tmp_path / "e.pdf")
        assert count_pdf_pages(pdf) == 0


class TestSourceTypeDispatch:
    def test_pdf_dispatches_to_pdf_renderer(self, tmp_path: Path) -> None:
        pdf = build_clean_english_pdf(tmp_path / "d.pdf", page_count=1)
        png = render_page_to_png(pdf, "pdf", 1)
        assert png.startswith(b"\x89PNG")

    def test_pptx_render_unsupported_in_mvp(self, tmp_path: Path) -> None:
        pptx = build_clean_english_pptx(tmp_path / "u.pptx", slide_count=1)
        with pytest.raises(UnsupportedRenderError):
            render_page_to_png(pptx, "pptx", 1)

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(UnsupportedRenderError):
            render_page_to_png("/tmp/x", "txt", 1)

    def test_count_pages_pdf(self, tmp_path: Path) -> None:
        pdf = build_clean_english_pdf(tmp_path / "c.pdf", page_count=4)
        assert count_pages(pdf, "pdf") == 4

    def test_count_pages_pptx(self, tmp_path: Path) -> None:
        pptx = build_clean_english_pptx(tmp_path / "c.pptx", slide_count=5)
        assert count_pages(pptx, "pptx") == 5

    def test_count_pages_docx_treats_as_one_page(self, tmp_path: Path) -> None:
        from tests.extractors.fixture_builders import build_clean_english_docx

        docx = build_clean_english_docx(tmp_path / "c.docx")
        assert count_pages(docx, "docx") == 1
