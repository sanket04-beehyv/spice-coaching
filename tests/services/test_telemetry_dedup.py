"""W-10 — telemetry_dedup tests (mocked Redis).

We don't run Redis in CI; the dedup module's Redis usage is small enough
to assert on call shapes via AsyncMock. A separate integration test
against a real Redis is left to the test-infra workstream.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from mc_contracts.enums import CoachingEventType, EventFamily
from mc_contracts.telemetry import TelemetryEvent
from platform_service.services import telemetry_dedup


def _evt(eid: str | None = None) -> TelemetryEvent:
    return TelemetryEvent(
        id=eid or str(uuid4()),
        event_family=EventFamily.COACHING,
        event_type=CoachingEventType.CARD_SHOWN,
        event_date=date.today(),
        timestamp_local=0,
    )


@pytest.mark.asyncio
async def test_is_first_seen_returns_true_when_redis_set_succeeds() -> None:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=True)
    out = await telemetry_dedup.is_first_seen(redis, "evt-1")
    assert out is True
    redis.set.assert_awaited_once()
    args, kwargs = redis.set.call_args
    assert args[0] == "telemetry:event:evt-1"
    assert kwargs == {"nx": True, "ex": 24 * 60 * 60}


@pytest.mark.asyncio
async def test_is_first_seen_returns_false_on_duplicate() -> None:
    redis = MagicMock()
    redis.set = AsyncMock(return_value=None)  # NX collision
    out = await telemetry_dedup.is_first_seen(redis, "evt-1")
    assert out is False


@pytest.mark.asyncio
async def test_partition_for_dedup_splits_first_seen_and_duplicates() -> None:
    e1, e2, e3 = _evt("a"), _evt("b"), _evt("c")
    pipe = MagicMock()
    pipe.set = MagicMock(return_value=pipe)
    # Redis pipeline returns one result per queued command, in order.
    pipe.execute = AsyncMock(return_value=[True, None, True])
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)

    first_seen, dupes = await telemetry_dedup.partition_for_dedup(redis, [e1, e2, e3])

    assert [e.id for e in first_seen] == ["a", "c"]
    assert dupes == ["b"]
    # Three queued SETs
    assert pipe.set.call_count == 3
    pipe.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_partition_for_dedup_empty_input_short_circuits() -> None:
    redis = MagicMock()
    redis.pipeline = MagicMock()
    out_first, out_dupes = await telemetry_dedup.partition_for_dedup(redis, [])
    assert out_first == []
    assert out_dupes == []
    redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_partition_preserves_input_order() -> None:
    """Order matters because the handler aligns event_ids with row indices."""
    events = [_evt(f"e{i}") for i in range(5)]
    pipe = MagicMock()
    pipe.set = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[True, True, None, True, None])
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    first_seen, dupes = await telemetry_dedup.partition_for_dedup(redis, events)
    assert [e.id for e in first_seen] == ["e0", "e1", "e3"]
    assert dupes == ["e2", "e4"]
