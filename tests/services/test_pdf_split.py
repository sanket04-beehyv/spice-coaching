"""Unit tests for physical PDF page-range splitting."""

from __future__ import annotations

from pathlib import Path

import pymupdf  # type: ignore[import-untyped]
import pytest
from platform_service.services.pdf_split import count_pdf_pages, split_pdf_page_range


def _make_pdf(path: Path, pages: int) -> None:
    with pymupdf.open() as doc:
        for index in range(pages):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {index + 1}")
        doc.save(str(path))


def test_count_pdf_pages(tmp_path: Path) -> None:
    pdf = tmp_path / "three.pdf"
    _make_pdf(pdf, 3)
    assert count_pdf_pages(pdf) == 3


def test_split_pdf_page_range_inclusive(tmp_path: Path) -> None:
    src = tmp_path / "src.pdf"
    dest = tmp_path / "split.pdf"
    _make_pdf(src, 4)
    split_pdf_page_range(src, start_page=2, end_page=3, dest_path=dest)
    assert count_pdf_pages(dest) == 2


def test_split_pdf_rejects_out_of_range(tmp_path: Path) -> None:
    src = tmp_path / "src.pdf"
    dest = tmp_path / "split.pdf"
    _make_pdf(src, 2)
    with pytest.raises(ValueError, match="exceeds"):
        split_pdf_page_range(src, start_page=1, end_page=3, dest_path=dest)


def test_split_pdf_rejects_inverted_range(tmp_path: Path) -> None:
    src = tmp_path / "src.pdf"
    dest = tmp_path / "split.pdf"
    _make_pdf(src, 3)
    with pytest.raises(ValueError, match="must be <="):
        split_pdf_page_range(src, start_page=3, end_page=1, dest_path=dest)
