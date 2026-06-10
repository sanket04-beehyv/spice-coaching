"""Telemetry ingest route tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from mc_contracts.enums import CoachingEventType, EventFamily
from platform_service.api.telemetry import router as telemetry_router
from platform_service.config import get_settings
from platform_service.deps import get_clickhouse_client

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


def _sample_batch_payload() -> dict:
    event_id = str(uuid4())
    return {
        "events": [
            {
                "id": event_id,
                "event_family": EventFamily.DIGITAL.value,
                "event_type": CoachingEventType.MODULE_DELIVERED.value,
                "module_id": str(uuid4()),
                "event_date": date.today().isoformat(),
                "timestamp_local": 1_700_000_000_000,
            }
        ],
        "sdk_version": "test-sdk",
        "chw_id": 42,
    }


@pytest_asyncio.fixture
async def app() -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(telemetry_router)
    app_obj.include_router(api_router)

    ch_mock = MagicMock()
    ch_mock.insert_coaching_events = AsyncMock()
    app_obj.dependency_overrides[get_clickhouse_client] = lambda: ch_mock

    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestTelemetryIngest:
    async def test_accepts_valid_batch(self, client: AsyncClient) -> None:
        redis_mock = AsyncMock()
        redis_mock.aclose = AsyncMock()
        body = _sample_batch_payload()

        async def _pass_through_dedup(_redis, events):  # type: ignore[no-untyped-def]
            return events, []

        with (
            patch("platform_service.api.telemetry._get_redis", return_value=redis_mock),
            patch(
                "platform_service.api.telemetry.partition_for_dedup",
                side_effect=_pass_through_dedup,
            ),
            patch("platform_service.api.telemetry.process_module_event_task") as celery_mock,
        ):
            resp = await client.post(platform_path("/telemetry/events"), json=body)

        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == [body["events"][0]["id"]]
        celery_mock.delay.assert_called_once()

    async def test_reports_duplicates(self, client: AsyncClient) -> None:
        redis_mock = AsyncMock()
        redis_mock.aclose = AsyncMock()
        dup_id = str(uuid4())
        with (
            patch("platform_service.api.telemetry._get_redis", return_value=redis_mock),
            patch(
                "platform_service.api.telemetry.partition_for_dedup",
                new=AsyncMock(return_value=([], [dup_id])),
            ),
            patch("platform_service.api.telemetry.process_module_event_task"),
        ):
            resp = await client.post(
                platform_path("/telemetry/events"),
                json={
                    "events": [],
                    "sdk_version": "test-sdk",
                    "chw_id": 1,
                },
            )

        assert resp.status_code == 200
        assert resp.json()["duplicates"] == [dup_id]
