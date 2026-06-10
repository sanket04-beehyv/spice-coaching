"""PDF text extraction utilities — no LLM, no AI dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


@dataclass
class Section:
    """One paragraph-level chunk from a BRAC health training PDF."""

    heading: str
    pages: list[int]
    full_text: str
    font_size: float = 0.0


class PdfExtractor:
    @staticmethod
    def extract_pages(file_path: str) -> list[tuple[int, str]]:
        """Return (1-based page number, page text) for every page with content."""
        path = Path(file_path)
        if path.suffix.lower() != ".pdf":
            raise ValueError("Only PDF files are supported")
        pages: list[tuple[int, str]] = []
        with fitz.open(file_path) as doc:
            for page in doc:
                text = page.get_text("text").strip()
                if text:
                    pages.append((page.number + 1, text))
        return pages

    @staticmethod
    def extract_sections(file_path: str, min_length: int = 200) -> list[Section]:
        """Split each page into paragraph-level sections for finer-grained extraction."""
        pages = PdfExtractor.extract_pages(file_path)
        sections: list[Section] = []
        min_para = max(80, min_length // 2)

        for page_num, text in pages:
            stripped = text.strip()
            if len(stripped) < min_length:
                continue
            blocks = [b.strip() for b in stripped.split("\n\n") if len(b.strip()) >= min_para]
            if not blocks:
                blocks = [stripped]
            for i, block in enumerate(blocks):
                if len(block) < min_length and len(blocks) > 1:
                    continue
                if len(block) < min_length:
                    continue
                sections.append(
                    Section(
                        heading=f"Page {page_num} §{i + 1}",
                        pages=[page_num - 1],
                        full_text=block,
                        font_size=0.0,
                    )
                )
        return sections
