"""Dashboard route tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.api.dashboard import router as dashboard_router
from platform_service.config import get_settings
from platform_service.deps import get_clickhouse_client

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(dashboard_router)
    app_obj.include_router(api_router)

    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(
        return_value=[
            {
                "cards_shown": 10,
                "quiz_attempts": 4,
                "quiz_correct": 3,
                "quiz_correct_rate": 0.75,
                "digital_help_used": 1,
                "incorrect_referrals": 0,
            }
        ]
    )
    app_obj.dependency_overrides[get_clickhouse_client] = lambda: ch_mock
    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestDashboardRoutes:
    async def test_supervisor_dashboard_returns_snapshot(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/dashboard/supervisor/99"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["chw_id"] == 99
        assert data["chw_snapshot"]["cards_shown"] == 10
        assert data["chw_snapshot"]["quiz_correct_rate"] == 0.75

    async def test_district_dashboard_not_implemented(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/dashboard/district/dhaka-north"))
        assert resp.status_code == 501
