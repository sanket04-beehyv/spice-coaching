"""Admin file upload route tests."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from platform_service.db.repositories.file_upload_repository import FileUploadRepository
from platform_service.deps import get_db, get_object_storage_client
from platform_service.main import create_app
from platform_service.services.object_storage import ObjectNotFoundError, StoredObject
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

_TEST_BUCKET = "test-bucket"
_TEST_PDF_BYTES = b"%PDF-1.4 hello"
_TEST_PDF_SHA256 = hashlib.sha256(_TEST_PDF_BYTES).hexdigest()


@pytest_asyncio.fixture
async def storage_mock() -> MagicMock:
    storage = MagicMock()
    storage.bucket_name = _TEST_BUCKET
    storage.put_object_from_local_file = AsyncMock(
        return_value=StoredObject(
            bucket_name=_TEST_BUCKET,
            object_name="uploads/test.txt",
            storage_path=f"{_TEST_BUCKET}/uploads/test.txt",
            content_type="application/pdf",
            size_bytes=5,
        )
    )
    storage.stat_object = AsyncMock()
    return storage


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
    storage_mock: MagicMock,
) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_object_storage_client] = lambda: storage_mock

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_file_upload(
    db_session: AsyncSession,
    *,
    content_sha256: str = _TEST_PDF_SHA256,
    object_key: str = "uploads/existing.pdf",
) -> None:
    repo = FileUploadRepository(db_session)
    await repo.upsert(
        bucket_name=_TEST_BUCKET,
        object_key=object_key,
        storage_path=f"{_TEST_BUCKET}/{object_key}",
        original_filename="existing.pdf",
        content_sha256=content_sha256,
        content_type="application/pdf",
        size_bytes=len(_TEST_PDF_BYTES),
        uploaded_by="test-user",
    )
    await db_session.flush()


class TestAdminFileUpload:
    async def test_upload_streams_file_to_storage(
        self,
        client: AsyncClient,
        storage_mock: MagicMock,
    ) -> None:
        files = {"file": ("hello.pdf", BytesIO(_TEST_PDF_BYTES), "application/pdf")}
        resp = await client.post(platform_path("/admin/v3/files"), files=files, data={"prefix": "uploads"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["storage_path"].startswith(f"{_TEST_BUCKET}/")
        assert body["content_type"] == "application/pdf"
        assert body["reused_existing"] is False
        storage_mock.put_object_from_local_file.assert_awaited_once()

    async def test_upload_reuses_existing_object_when_hash_matches(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        storage_mock: MagicMock,
    ) -> None:
        await _seed_file_upload(db_session)
        files = {"file": ("hello.pdf", BytesIO(_TEST_PDF_BYTES), "application/pdf")}
        resp = await client.post(platform_path("/admin/v3/files"), files=files, data={"prefix": "uploads"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["reused_existing"] is True
        assert body["storage_path"] == f"{_TEST_BUCKET}/uploads/existing.pdf"
        assert body["object_name"] == "uploads/existing.pdf"
        storage_mock.put_object_from_local_file.assert_not_awaited()
        storage_mock.stat_object.assert_awaited_once_with("uploads/existing.pdf")

    async def test_upload_falls_back_when_existing_object_missing_from_storage(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        storage_mock: MagicMock,
    ) -> None:
        await _seed_file_upload(db_session)
        storage_mock.stat_object = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        files = {"file": ("hello.pdf", BytesIO(_TEST_PDF_BYTES), "application/pdf")}
        resp = await client.post(platform_path("/admin/v3/files"), files=files, data={"prefix": "uploads"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["reused_existing"] is False
        storage_mock.put_object_from_local_file.assert_awaited_once()
