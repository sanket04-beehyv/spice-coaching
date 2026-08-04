"""Device sync route smoke tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.api.sync import router as sync_router
from platform_service.config import get_settings
from platform_service.deps import get_db, get_object_storage_client
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.conftest import (
    _mock_storage,
    _seed_module,
    _seed_source_document,
)
from tests.conftest import platform_path, requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(
        db_session, "module_quiz_question, module, module_family, content_block, source_page, source_document"
    )
    yield


class _FakeStorage:
    bucket_name = "medtronics-storage"
    allowed_prefixes = frozenset({"uploads", "source-documents"})

    async def presigned_get_url(self, **kwargs):  # type: ignore[no-untyped-def]
        return type("Url", (), {"url": "https://example.test/obj", "expires_at": datetime.now(UTC)})()


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(sync_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    app_obj.dependency_overrides[get_object_storage_client] = lambda: _FakeStorage()
    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestSyncRoutes:
    async def test_config_sync_returns_bundle(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/sync/config"))
        assert resp.status_code == 200
        data = resp.json()
        assert "thresholds" in data
        assert "locales" in data
        locales = data["locales"]
        settings = get_settings()
        assert locales["primary"] == settings.deployment_primary_locale
        assert "mirror" not in locales
        assert locales["supported"] == settings.deployment_locale_config.supported

    async def test_modules_sync_requires_since(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/sync/modules"))
        assert resp.status_code == 422

    async def test_modules_sync_empty_when_no_updates(self, client: AsyncClient) -> None:
        since = datetime.now(UTC).isoformat()
        resp = await client.get(platform_path("/sync/modules"), params={"since": since})
        assert resp.status_code == 200
        data = resp.json()
        assert data["modules"] == []
        assert data["assigned_module_ids"] == []
        assert data["requested_modules"] == []


class TestPublishedSourceDocuments:
    async def test_returns_visible_documents_without_module_link(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        doc = await _seed_source_document(db_session, sync_published_visible=True)

        resp = await client.get(platform_path("/sync/source-documents/published"))
        assert resp.status_code == 200
        data = resp.json()
        assert "server_time_utc" in data
        assert "modules" not in data
        assert len(data["source_documents"]) == 1
        entry = data["source_documents"][0]
        assert entry["source_document_id"] == str(doc.id)
        assert entry["title"] == doc.title
        assert "cards" not in data
        assert "module_cards" not in data

    async def test_presigns_source_document_and_thumbnail(
        self, app: FastAPI, db_session: AsyncSession
    ) -> None:
        doc = await _seed_source_document(db_session, title="RMNCH Manual", sync_published_visible=True)
        doc.thumbnail_storage_path = "medtronics-storage/ingest/thumbnails/manual.png"
        await db_session.commit()

        thumb_url = "https://minio.example/thumb.png"
        mock_storage = _mock_storage(presigned_url=thumb_url)
        app.dependency_overrides[get_object_storage_client] = lambda: mock_storage

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(platform_path("/sync/source-documents/published"))

        assert resp.status_code == 200
        entry = resp.json()["source_documents"][0]
        assert entry["presigned_url"] == thumb_url
        assert entry["presigned_expires_seconds"] == get_settings().admin_file_presigned_max_seconds
        assert "thumbnail_storage_path" not in entry
        assert entry["thumbnail_presigned_url"] == thumb_url
        assert entry["thumbnail_presigned_expires_seconds"] == get_settings().admin_file_presigned_max_seconds

    async def test_excludes_documents_when_sync_published_visible_false(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _seed_source_document(db_session, sync_published_visible=False)
        await _seed_module(db_session)

        resp = await client.get(platform_path("/sync/source-documents/published"))
        assert resp.status_code == 200
        assert resp.json()["source_documents"] == []
