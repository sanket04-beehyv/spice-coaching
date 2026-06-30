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
from mc_contracts.telemetry import TelemetryEvent
from platform_service.api.telemetry import (
    _event_to_row,
    _resolve_timestamp_utc,
)
from platform_service.api.telemetry import (
    router as telemetry_router,
)
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
                "timestamp_local": 1_700_000_000,
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


_TIMESTAMP_UTC_COLUMN_INDEX = 29


class TestResolveTimestampUtc:
    def test_returns_timestamp_utc_when_provided(self) -> None:
        event = TelemetryEvent(
            id="evt-1",
            event_family=EventFamily.COACHING,
            event_type="module_viewed",
            event_date=date.today(),
            timestamp_local=1_700_000_000,
            timestamp_utc=1_699_000_000,
        )
        assert _resolve_timestamp_utc(event) == 1_699_000_000

    def test_falls_back_to_timestamp_local_when_utc_omitted(self) -> None:
        event = TelemetryEvent(
            id="evt-1",
            event_family=EventFamily.COACHING,
            event_type="module_viewed",
            event_date=date.today(),
            timestamp_local=1_700_000_000,
        )
        assert _resolve_timestamp_utc(event) == 1_700_000_000

    def test_event_to_row_uses_resolved_timestamp_utc(self) -> None:
        event = TelemetryEvent(
            id="evt-1",
            event_family=EventFamily.COACHING,
            event_type="module_viewed",
            event_date=date.today(),
            timestamp_local=1_700_000_000,
        )
        row = _event_to_row(
            e=event,
            sdk_version="test-sdk",
            chw_id=42,
            tenant_id=None,
            synced_at_ms=1_700_000_001_000,
        )
        assert row[_TIMESTAMP_UTC_COLUMN_INDEX] == 1_700_000_000


class TestTelemetryIngest:
    async def test_accepts_valid_batch(self, app: FastAPI, client: AsyncClient) -> None:
        redis_mock = AsyncMock()
        redis_mock.aclose = AsyncMock()
        body = _sample_batch_payload()
        ch_mock = app.dependency_overrides[get_clickhouse_client]()

        async def _pass_through_dedup(_redis, events):  # type: ignore[no-untyped-def]
            return events, []

        with (
            patch("platform_service.api.telemetry.get_redis_client", return_value=redis_mock),
            patch(
                "platform_service.api.telemetry.partition_for_dedup",
                side_effect=_pass_through_dedup,
            ),
            patch("platform_service.api.telemetry.process_module_event_task") as celery_mock,
            patch.object(
                get_settings(),
                "telemetry_behavioural_gap_state_enabled",
                True,
            ),
        ):
            resp = await client.post(platform_path("/telemetry/events"), json=body)

        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == [body["events"][0]["id"]]
        celery_mock.delay.assert_called_once()
        ch_mock.insert_coaching_events.assert_awaited_once()
        rows = ch_mock.insert_coaching_events.await_args.args[0]
        assert rows[0][_TIMESTAMP_UTC_COLUMN_INDEX] == body["events"][0]["timestamp_local"]

    async def test_quiz_mode_only_enqueues_module_quiz_attempted(self, client: AsyncClient) -> None:
        redis_mock = AsyncMock()
        redis_mock.aclose = AsyncMock()
        module_id = str(uuid4())
        quiz_id = str(uuid4())
        delivered_id = str(uuid4())
        quiz_event_id = str(uuid4())
        body = {
            "events": [
                {
                    "id": delivered_id,
                    "event_family": EventFamily.DIGITAL.value,
                    "event_type": CoachingEventType.MODULE_DELIVERED.value,
                    "module_id": module_id,
                    "event_date": date.today().isoformat(),
                    "timestamp_local": 1_700_000_000,
                },
                {
                    "id": quiz_event_id,
                    "event_family": EventFamily.DIGITAL.value,
                    "event_type": CoachingEventType.MODULE_QUIZ_ATTEMPTED.value,
                    "module_id": module_id,
                    "quiz_id": quiz_id,
                    "quiz_score_pct": 0.5,
                    "event_date": date.today().isoformat(),
                    "timestamp_local": 1_700_000_001,
                },
            ],
            "sdk_version": "test-sdk",
            "chw_id": 42,
        }

        async def _pass_through_dedup(_redis, events):  # type: ignore[no-untyped-def]
            return events, []

        with (
            patch("platform_service.api.telemetry.get_redis_client", return_value=redis_mock),
            patch(
                "platform_service.api.telemetry.partition_for_dedup",
                side_effect=_pass_through_dedup,
            ),
            patch("platform_service.api.telemetry.process_module_event_task") as celery_mock,
            patch.object(
                get_settings(),
                "telemetry_behavioural_gap_state_enabled",
                False,
            ),
        ):
            resp = await client.post(platform_path("/telemetry/events"), json=body)

        assert resp.status_code == 200
        assert celery_mock.delay.call_count == 1
        job = celery_mock.delay.call_args.args[0]
        assert job["event_id"] == quiz_event_id
        assert job["event_type"] == CoachingEventType.MODULE_QUIZ_ATTEMPTED.value

    async def test_reports_duplicates(self, client: AsyncClient) -> None:
        redis_mock = AsyncMock()
        redis_mock.aclose = AsyncMock()
        dup_id = str(uuid4())
        with (
            patch("platform_service.api.telemetry.get_redis_client", return_value=redis_mock),
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
