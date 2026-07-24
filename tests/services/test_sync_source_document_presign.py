"""SyncService.get_source_document_presigned_urls — batch presign for device sync."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from platform_service.config import Settings
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.object_storage import ObjectNotFoundError, PresignedObjectUrl
from platform_service.services.sync_service import SyncService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]

_BUCKET = "medtronics-storage"
_OBJECT_KEY = "source-documents/abc_manual.pdf"
_STORAGE_PATH = f"{_BUCKET}/{_OBJECT_KEY}"


@pytest_asyncio.fixture(autouse=True)
async def _wipe_source_documents(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(db_session, "source_document")
    yield


async def _seed_doc(
    session: AsyncSession,
    *,
    storage_path: str = _STORAGE_PATH,
    original_filename: str | None = "manual.pdf",
) -> UUID:
    doc = SourceDocument(
        title="sync-presign-test",
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        assessment_mode="with_quiz",
        original_storage_path=storage_path,
        original_filename=original_filename,
    )
    session.add(doc)
    await session.flush()
    await session.commit()
    return doc.id


def _mock_storage(*, presigned_url: str = "https://minio.example/presigned") -> MagicMock:
    storage = MagicMock()
    storage.presigned_get_url = AsyncMock(
        return_value=PresignedObjectUrl(
            url=presigned_url,
            bucket_name=_BUCKET,
            object_name=_OBJECT_KEY,
            expires_seconds=86400,
        )
    )
    return storage


@pytest.mark.asyncio
@requires_db
async def test_presign_all_found(db_session: AsyncSession) -> None:
    doc_id = await _seed_doc(db_session)
    storage = _mock_storage()
    settings = Settings()

    resp = await SyncService(db_session).get_source_document_presigned_urls(
        source_document_ids=[doc_id],
        storage=storage,
        settings=settings,
    )

    assert resp.missing_ids == []
    assert len(resp.urls) == 1
    assert resp.urls[0].source_document_id == doc_id
    assert resp.urls[0].storage_path == _STORAGE_PATH
    assert resp.urls[0].presigned_url == "https://minio.example/presigned"
    assert resp.urls[0].expires_seconds == settings.admin_file_presigned_max_seconds
    storage.presigned_get_url.assert_awaited_once_with(
        object_name=_STORAGE_PATH,
        expires_seconds=settings.admin_file_presigned_max_seconds,
        download_filename="manual.pdf",
    )


@pytest.mark.asyncio
@requires_db
async def test_presign_unknown_id_in_missing(db_session: AsyncSession) -> None:
    doc_id = await _seed_doc(db_session)
    unknown_id = uuid4()
    storage = _mock_storage()

    resp = await SyncService(db_session).get_source_document_presigned_urls(
        source_document_ids=[doc_id, unknown_id],
        storage=storage,
    )

    assert len(resp.urls) == 1
    assert resp.urls[0].source_document_id == doc_id
    assert resp.missing_ids == [unknown_id]


@pytest.mark.asyncio
@requires_db
async def test_presign_legacy_filesystem_path_in_missing(db_session: AsyncSession) -> None:
    doc_id = await _seed_doc(db_session, storage_path="/tmp/legacy.pdf")
    storage = _mock_storage()

    resp = await SyncService(db_session).get_source_document_presigned_urls(
        source_document_ids=[doc_id],
        storage=storage,
    )

    assert resp.urls == []
    assert resp.missing_ids == [doc_id]
    storage.presigned_get_url.assert_not_awaited()


@pytest.mark.asyncio
@requires_db
async def test_presign_missing_object_in_missing(db_session: AsyncSession) -> None:
    doc_id = await _seed_doc(db_session)
    storage = _mock_storage()
    storage.presigned_get_url = AsyncMock(side_effect=ObjectNotFoundError("missing"))

    resp = await SyncService(db_session).get_source_document_presigned_urls(
        source_document_ids=[doc_id],
        storage=storage,
    )

    assert resp.urls == []
    assert resp.missing_ids == [doc_id]


@pytest.mark.asyncio
@requires_db
async def test_presign_empty_request(db_session: AsyncSession) -> None:
    storage = _mock_storage()

    resp = await SyncService(db_session).get_source_document_presigned_urls(
        source_document_ids=[],
        storage=storage,
    )

    assert resp.urls == []
    assert resp.missing_ids == []
    storage.presigned_get_url.assert_not_awaited()
