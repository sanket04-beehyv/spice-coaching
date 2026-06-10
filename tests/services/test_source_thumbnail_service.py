"""Unit tests for source_document thumbnail generation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from platform_service.services.source_thumbnail_service import (
    SourceThumbnailService,
    render_thumbnail_png_bytes,
    thumbnail_object_name,
)
from platform_service.workers.extractors.page_renderer import UnsupportedRenderError


def test_render_thumbnail_png_bytes_docx_skipped(tmp_path: Path) -> None:
    path = tmp_path / "doc.docx"
    path.write_bytes(b"not-a-real-docx")
    with pytest.raises(UnsupportedRenderError, match="thumbnail_skipped"):
        render_thumbnail_png_bytes(path, "docx")


def test_render_thumbnail_png_bytes_pptx_skipped(tmp_path: Path) -> None:
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"not-a-real-pptx")
    with pytest.raises(UnsupportedRenderError, match="thumbnail_skipped"):
        render_thumbnail_png_bytes(path, "pptx")


def test_thumbnail_object_name() -> None:
    doc_id = uuid4()
    assert thumbnail_object_name(doc_id) == f"ingest/thumbnails/{doc_id}.png"


@pytest.mark.asyncio
async def test_generate_and_store_skips_docx_without_upload() -> None:
    doc_id = uuid4()
    doc = MagicMock()
    doc.thumbnail_storage_path = None
    session = AsyncMock()
    repo = MagicMock()
    repo.get_source_document = AsyncMock(return_value=doc)
    storage = MagicMock()
    storage.put_object_from_local_file = AsyncMock()

    service = SourceThumbnailService(session, storage=storage)
    service._repo = repo

    result = await service.generate_and_store(
        source_document_id=doc_id,
        source_path="bucket/ingest/x.docx",
        source_type="docx",
    )
    assert result is None
    storage.put_object_from_local_file.assert_not_called()
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_generate_and_store_uploads_pdf_and_persists() -> None:
    doc_id = uuid4()
    doc = MagicMock()
    doc.thumbnail_storage_path = None
    session = AsyncMock()
    repo = MagicMock()
    repo.get_source_document = AsyncMock(return_value=doc)
    repo.update_thumbnail_storage_path = AsyncMock()

    stored = MagicMock()
    stored.storage_path = "medtronics-storage/ingest/thumbnails/x.png"
    storage = MagicMock()
    storage.put_object_from_local_file = AsyncMock(return_value=stored)

    local = Path("/tmp/fake.pdf")
    fake_png = b"\x89PNG\r\n\x1a\n"

    service = SourceThumbnailService(session, storage=storage)
    service._repo = repo
    service._settings.minio_bucket_name = "medtronics-storage"

    with (
        patch(
            "platform_service.services.source_thumbnail_service.materialize_local_source_file",
            new_callable=AsyncMock,
            return_value=(local, local),
        ),
        patch(
            "platform_service.services.source_thumbnail_service.render_thumbnail_png_bytes",
            return_value=fake_png,
        ),
    ):
        result = await service.generate_and_store(
            source_document_id=doc_id,
            source_path="medtronics-storage/ingest/file.pdf",
            source_type="pdf",
        )

    assert result == f"medtronics-storage/{thumbnail_object_name(doc_id)}"
    storage.put_object_from_local_file.assert_awaited_once()
    repo.update_thumbnail_storage_path.assert_awaited_once()
    session.commit.assert_awaited_once()
