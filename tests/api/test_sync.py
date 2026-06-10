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

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


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

    async def test_modules_sync_requires_since(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/sync/modules"))
        assert resp.status_code == 422

    async def test_modules_sync_empty_when_no_updates(self, client: AsyncClient) -> None:
        since = datetime.now(UTC).isoformat()
        resp = await client.get(platform_path("/sync/modules"), params={"since": since})
        assert resp.status_code == 200
        assert resp.json()["modules"] == []
