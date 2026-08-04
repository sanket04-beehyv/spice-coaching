"""Physical PDF page-range splitting via pymupdf."""

from __future__ import annotations

from pathlib import Path

import pymupdf  # type: ignore[import-untyped]


def count_pdf_pages(pdf_path: str | Path) -> int:
    """Return the number of pages in ``pdf_path``."""
    with pymupdf.open(str(pdf_path)) as doc:
        return len(doc)


def split_pdf_page_range(
    pdf_path: str | Path,
    *,
    start_page: int,
    end_page: int,
    dest_path: str | Path,
) -> None:
    """Write pages ``start_page``..``end_page`` (1-based inclusive) to ``dest_path``.

    Raises:
        ValueError: when the range is empty, inverted, or outside the document.
    """
    path = Path(pdf_path)
    dest = Path(dest_path)
    with pymupdf.open(str(path)) as src:
        page_count = len(src)
        if start_page < 1 or end_page < 1:
            raise ValueError(f"page numbers must be >= 1; got start={start_page}, end={end_page}")
        if start_page > end_page:
            raise ValueError(f"start_page ({start_page}) must be <= end_page ({end_page})")
        if end_page > page_count:
            raise ValueError(f"end_page ({end_page}) exceeds document page count ({page_count})")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open() as dst:
            dst.insert_pdf(src, from_page=start_page - 1, to_page=end_page - 1)
            dst.save(str(dest))
