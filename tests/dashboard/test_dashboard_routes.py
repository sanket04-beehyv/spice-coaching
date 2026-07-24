"""Dashboard route tests that do not require PostgreSQL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from mc_contracts.dashboard import (
    DigitalHelpModuleUsageItem,
    DigitalHelpModuleUsageResponse,
)
from platform_service.api.dashboard import router as dashboard_router
from platform_service.config import get_settings
from platform_service.deps import get_clickhouse_client, get_db

from tests.conftest import platform_path

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(dashboard_router)
    app_obj.include_router(api_router)

    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(return_value=[])
    app_obj.dependency_overrides[get_clickhouse_client] = lambda: ch_mock

    async def _override_get_db() -> AsyncIterator[MagicMock]:
        yield MagicMock()

    app_obj.dependency_overrides[get_db] = _override_get_db
    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestDigitalHelpModuleUsageRoute:
    @patch(
        "platform_service.api.dashboard.DashboardAnalyticsService.get_digital_help_module_usage",
        new_callable=AsyncMock,
    )
    async def test_digital_help_modules_returns_ranked_modules(
        self,
        mock_get: AsyncMock,
        client: AsyncClient,
    ) -> None:
        family_a = uuid4()
        family_b = uuid4()
        module_a_id = uuid4()
        module_b_id = uuid4()
        mock_get.return_value = DigitalHelpModuleUsageResponse(
            period_days=30,
            total_queries=13,
            total_modules=2,
            limit=10,
            offset=0,
            modules=[
                DigitalHelpModuleUsageItem(
                    module_id=module_a_id,
                    module_family_id=family_a,
                    query_count=10,
                    title={"bn": "Module A", "en": "Module A EN"},
                ),
                DigitalHelpModuleUsageItem(
                    module_id=module_b_id,
                    module_family_id=family_b,
                    query_count=3,
                    title={"bn": "Module B"},
                ),
            ],
        )

        resp = await client.get(
            platform_path("/dashboard/digital-help-modules?period_days=30&limit=10&offset=0")
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["period_days"] == 30
        assert data["total_queries"] == 13
        assert data["total_modules"] == 2
        assert data["limit"] == 10
        assert data["offset"] == 0
        assert len(data["modules"]) == 2
        assert data["modules"][0]["query_count"] == 10
        assert data["modules"][0]["module_id"] == str(module_a_id)
        assert data["modules"][0]["title"]["bn"] == "Module A"
        mock_get.assert_awaited_once()
        assert mock_get.await_args.kwargs["period_days"] == 30
        assert mock_get.await_args.kwargs["limit"] == 10
        assert mock_get.await_args.kwargs["offset"] == 0
