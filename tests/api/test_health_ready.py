"""Readiness endpoint tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from platform_service.main import create_app

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_ready_returns_ok_when_dependencies_healthy(client: AsyncClient) -> None:
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(return_value=[(1,)])
    storage_mock = MagicMock()
    storage_mock.check_readiness = AsyncMock()

    with (
        patch("platform_service.main.get_clickhouse_client", return_value=ch_mock),
        patch("platform_service.main.get_object_storage_client", return_value=storage_mock),
        patch("platform_service.main.httpx.AsyncClient") as httpx_cls,
    ):
        ai_client = AsyncMock()
        ai_resp = MagicMock()
        ai_resp.raise_for_status = MagicMock()
        ai_resp.json = MagicMock(return_value={"provider": "google"})
        ai_client.get = AsyncMock(return_value=ai_resp)
        ai_client.__aenter__ = AsyncMock(return_value=ai_client)
        ai_client.__aexit__ = AsyncMock(return_value=None)
        httpx_cls.return_value = ai_client

        resp = await client.get(platform_path("/ready"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["clickhouse"] == "ok"
    assert body["checks"]["ai_runtime"] == "ok"
    assert body["checks"]["object_storage"] == "ok"
