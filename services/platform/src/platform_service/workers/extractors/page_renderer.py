"""W-2 Stage A — page-to-PNG rendering.

Per Pipeline v3.3 §4.5. Renders source pages to PNG at 2× DPI (configurable).
Used both for vision fallback (vision_extractor consumes the bytes) and for
the reviewer drill-down UI (lazy-rendered for text-path pages).

PDF: pymupdf renders directly.
PPTX: python-pptx doesn't render to images; we use the original slide image
  representation by snapshotting the slide via libreoffice (subprocess) when
  available. In MVP, pptx ingestion uses text-only path so vision rendering
  is rare; we surface a clear NotImplementedError to flag the case.
DOCX: same shape — pre-converted to PDF via libreoffice if vision fallback
  is needed.

For pilot scope (UHIS pptx + BRAC English/Bangla manuals), the only path
that exercises this in production is PDF rendering.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

import pymupdf  # type: ignore[import-untyped]

from platform_service.config import get_settings

logger = logging.getLogger(__name__)

# 2× DPI rendering matrix.
_DEFAULT_ZOOM = 2.0


class UnsupportedRenderError(NotImplementedError):
    """Raised when render-to-PNG is requested for a source type with no
    available rendering backend in this environment."""


def render_pdf_page_to_png(pdf_path: str | Path, page_number: int, zoom: float = _DEFAULT_ZOOM) -> bytes:
    """Render one PDF page to PNG bytes. page_number is 1-indexed."""
    path = Path(pdf_path)
    with pymupdf.open(str(path)) as doc:
        if page_number < 1 or page_number > len(doc):
            raise IndexError(f"page_number {page_number} out of range for {path.name} (1..{len(doc)})")
        page = doc[page_number - 1]
        mat = pymupdf.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")


def count_pdf_pages(pdf_path: str | Path) -> int:
    with pymupdf.open(str(pdf_path)) as doc:
        return len(doc)


def render_page_to_png(source_path: str | Path, source_type: str, page_number: int) -> bytes:
    """Source-type-aware dispatcher.

    Raises UnsupportedRenderError for source types that need an external
    renderer (libreoffice subprocess) which isn't wired into MVP.
    """
    if source_type == "pdf":
        return render_pdf_page_to_png(source_path, page_number)
    if source_type in ("pptx", "docx"):
        raise UnsupportedRenderError(
            f"render_page_to_png for {source_type!r} requires libreoffice export; "
            "not wired in MVP because text-only path covers the pilot pptx/docx sources"
        )
    raise UnsupportedRenderError(f"Unsupported source_type for rendering: {source_type!r}")


def page_image_path(source_document_id: UUID, page_number: int) -> Path:
    """Filesystem path for a cached rendered page PNG."""
    settings = get_settings()
    return (
        Path(settings.upload_dir) / "source_pages" / str(source_document_id) / f"page_{page_number:04d}.png"
    )


def persist_rendered_page_image(source_document_id: UUID, page_number: int, png_bytes: bytes) -> str:
    """Write the page PNG under upload_dir/source_pages/{document_id}/."""
    path = page_image_path(source_document_id, page_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes)
    return str(path)


def load_cached_page_png(source_document_id: UUID, page_number: int) -> bytes | None:
    """Return cached PNG bytes when the rendered page image already exists."""
    cached = page_image_path(source_document_id, page_number)
    if not cached.is_file():
        return None
    try:
        return cached.read_bytes()
    except OSError:
        return None


def count_pages(source_path: str | Path, source_type: str) -> int:
    """Source-type-aware page count."""
    if source_type == "pdf":
        return count_pdf_pages(source_path)
    if source_type == "pptx":
        from pptx import Presentation  # type: ignore[import-untyped]

        return len(Presentation(str(source_path)).slides)
    if source_type == "docx":
        # docx has no inherent "page" concept; we treat the whole document as
        # one logical "page" for ingestion purposes. Real per-page text
        # extraction is approximated by section breaks in text_extractor.
        return 1
    if source_type in ("audio", "video"):
        # Media transcription produces one logical transcript page.
        return 1
    raise UnsupportedRenderError(f"Unsupported source_type for page count: {source_type!r}")
