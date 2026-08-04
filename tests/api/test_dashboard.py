"""Dashboard route tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from mc_foundation.problem import register_problem_handlers
from platform_service.api.dashboard import router as dashboard_router
from platform_service.config import get_settings
from platform_service.deps import get_db

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    register_problem_handlers(
        app_obj,
        validation_error_type=RequestValidationError,
        http_exception_type=HTTPException,
    )
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
    app_obj.state.ch_mock = ch_mock
    monkeypatch.setattr("platform_service.api.dashboard.get_clickhouse_client", lambda: ch_mock)

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

    async def test_document_usage_combined_response(
        self, app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        doc_id = uuid4()
        ch_mock = app.state.ch_mock
        ch_mock.query_rows = AsyncMock(
            side_effect=[
                [{"total_views": 7, "unique_documents": 1, "unique_users": 2}],
                [{"source_document_id": str(doc_id), "view_count": 7}],
                [{"total_document_rows": 1}],
                [
                    {
                        "source_document_id": str(doc_id),
                        "total_views": 7,
                        "unique_users": 2,
                    }
                ],
                [
                    {
                        "source_document_id": str(doc_id),
                        "last_chw_id": 401,
                        "last_viewed_at": None,
                    }
                ],
                [{"total_events": 1}],
                [
                    {
                        "event_id": "evt-view-1",
                        "source_document_id": str(doc_id),
                        "chw_id": 395,
                        "upazila_id": "Lalmonirhat Sadar",
                        "viewed_at": None,
                    }
                ],
            ]
        )
        monkeypatch.setattr(
            "platform_service.services.document_usage_analytics_service.SourceRepository.list_source_documents_by_ids",
            AsyncMock(return_value=[]),
        )
        resp = await client.get(
            platform_path("/dashboard/document-usage"),
            params={"from": "2026-04-01", "to": "2026-04-30"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_views"] == 7
        assert data["unique_documents"] == 1
        assert data["unique_users"] == 2
        assert data["top_documents"][0]["document_id"] == str(doc_id)
        assert data["top_documents"][0]["view_count"] == 7
        assert data["total_document_rows"] == 1
        assert data["documents"][0]["total_views"] == 7
        assert data["documents"][0]["last_viewed_by_user_id"] == 401
        assert data["total_events"] == 1
        assert data["events"][0]["event_id"] == "evt-view-1"
        assert data["events"][0]["user_role"] == "SK"

    async def test_document_usage_rejects_inverted_dates(self, client: AsyncClient) -> None:
        resp = await client.get(
            platform_path("/dashboard/document-usage"),
            params={"from": "2026-04-30", "to": "2026-04-01"},
        )
        assert resp.status_code == 422
