"""SyncService.get_source_document_thumbnail_presigned_urls — batch presign for device sync."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from platform_service.config import Settings
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.sync_service import SyncService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

_BUCKET = "medtronics-storage"
_STORAGE_PATH = f"{_BUCKET}/source-documents/manual.pdf"
_THUMB_PATH = f"{_BUCKET}/ingest/thumbnails/{uuid4()}.png"


@pytest_asyncio.fixture(autouse=True)
async def _rollback_source_documents(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()


async def _seed_source_document(
    session: AsyncSession,
    *,
    thumbnail_storage_path: str | None,
) -> SourceDocument:
    doc = SourceDocument(
        title="sync-thumb-test",
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        assessment_mode="with_quiz",
        version_label="2026-Q1",
        publication_date=date(2026, 1, 15),
        original_storage_path=_STORAGE_PATH,
        original_filename="manual.pdf",
        thumbnail_storage_path=thumbnail_storage_path,
    )
    session.add(doc)
    await session.flush()
    await session.commit()
    return doc


def _mock_storage() -> MagicMock:
    storage = MagicMock()
    storage.presigned_get_url = AsyncMock()
    return storage


@pytest.mark.asyncio
@requires_db
async def test_presign_source_document_thumbnail_found(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session, thumbnail_storage_path=_THUMB_PATH)
    storage = _mock_storage()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "platform_service.services.source_thumbnail_service.presign_thumbnail",
            AsyncMock(return_value=("https://minio.example/thumb", 600)),
        )
        resp = await SyncService(db_session).get_source_document_thumbnail_presigned_urls(
            source_document_ids=[doc.id],
            storage=storage,
            settings=Settings(minio_bucket_name=_BUCKET),
        )

    assert len(resp.urls) == 1
    assert resp.urls[0].source_document_id == doc.id
    assert resp.urls[0].storage_path == _THUMB_PATH
    assert resp.urls[0].presigned_url == "https://minio.example/thumb"
    assert resp.urls[0].expires_seconds == 600
    assert resp.missing_ids == []


@pytest.mark.asyncio
@requires_db
async def test_presign_source_document_thumbnail_missing_when_no_path(
    db_session: AsyncSession,
) -> None:
    doc = await _seed_source_document(db_session, thumbnail_storage_path=None)
    storage = _mock_storage()

    resp = await SyncService(db_session).get_source_document_thumbnail_presigned_urls(
        source_document_ids=[doc.id],
        storage=storage,
        settings=Settings(minio_bucket_name=_BUCKET),
    )

    assert resp.urls == []
    assert resp.missing_ids == [doc.id]


@pytest.mark.asyncio
@requires_db
async def test_presign_source_document_thumbnail_unknown_id(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session, thumbnail_storage_path=_THUMB_PATH)
    unknown_id = uuid4()
    storage = _mock_storage()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "platform_service.services.source_thumbnail_service.presign_thumbnail",
            AsyncMock(return_value=("https://minio.example/thumb", 600)),
        )
        resp = await SyncService(db_session).get_source_document_thumbnail_presigned_urls(
            source_document_ids=[doc.id, unknown_id],
            storage=storage,
            settings=Settings(minio_bucket_name=_BUCKET),
        )

    assert len(resp.urls) == 1
    assert resp.urls[0].source_document_id == doc.id
    assert resp.missing_ids == [unknown_id]


@pytest.mark.asyncio
@requires_db
async def test_presign_source_document_thumbnail_empty_request(db_session: AsyncSession) -> None:
    storage = _mock_storage()

    resp = await SyncService(db_session).get_source_document_thumbnail_presigned_urls(
        source_document_ids=[],
        storage=storage,
        settings=Settings(minio_bucket_name=_BUCKET),
    )

    assert resp.urls == []
    assert resp.missing_ids == []
    storage.presigned_get_url.assert_not_awaited()
