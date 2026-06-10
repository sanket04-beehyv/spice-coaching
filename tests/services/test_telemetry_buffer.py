"""W-10 — telemetry_buffer tests (mocked Redis)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from platform_service.services import telemetry_buffer


@pytest.mark.asyncio
async def test_enqueue_rows_pushes_to_correct_key_and_caps_length() -> None:
    pipe = MagicMock()
    pipe.rpush = MagicMock(return_value=pipe)
    pipe.ltrim = MagicMock(return_value=pipe)
    pipe.llen = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[2, "OK", 42])
    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)

    rows = [["a", 1], ["b", 2]]
    n = await telemetry_buffer.enqueue_rows(redis, table="coaching_events", rows=rows, event_ids=["e1", "e2"])
    assert n == 2

    rpush_args = pipe.rpush.call_args
    assert rpush_args.args[0] == "telemetry:retry:coaching_events"
    payloads = list(rpush_args.args[1:])
    assert json.loads(payloads[0]) == {"row": ["a", 1], "event_id": "e1"}
    assert json.loads(payloads[1]) == {"row": ["b", 2], "event_id": "e2"}
    # ltrim caps at the configured max (negative-index form: keep last N)
    pipe.ltrim.assert_called_once()
    ltrim_args = pipe.ltrim.call_args.args
    assert ltrim_args[0] == "telemetry:retry:coaching_events"
    assert ltrim_args[2] == -1
    assert ltrim_args[1] == -telemetry_buffer._MAX_QUEUE_LENGTH


@pytest.mark.asyncio
async def test_enqueue_rows_empty_input_short_circuits() -> None:
    redis = MagicMock()
    redis.pipeline = MagicMock()
    n = await telemetry_buffer.enqueue_rows(redis, table="coaching_events", rows=[])
    assert n == 0
    redis.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_enqueue_rows_event_ids_length_mismatch_raises() -> None:
    redis = MagicMock()
    with pytest.raises(ValueError, match="event_ids length must match"):
        await telemetry_buffer.enqueue_rows(
            redis,
            table="coaching_events",
            rows=[["a"], ["b"]],
            event_ids=["only-one"],
        )


@pytest.mark.asyncio
async def test_drain_batch_returns_rows_and_event_ids() -> None:
    redis = MagicMock()
    redis.lpop = AsyncMock(
        return_value=[
            json.dumps({"row": ["a", 1], "event_id": "e1"}),
            json.dumps({"row": ["b", 2], "event_id": "e2"}),
        ]
    )
    rows, eids = await telemetry_buffer.drain_batch(redis, table="coaching_events", batch_size=10)
    assert rows == [["a", 1], ["b", 2]]
    assert eids == ["e1", "e2"]
    redis.lpop.assert_awaited_once_with("telemetry:retry:coaching_events", 10)


@pytest.mark.asyncio
async def test_drain_batch_empty_queue_returns_empty_lists() -> None:
    redis = MagicMock()
    redis.lpop = AsyncMock(return_value=None)
    rows, eids = await telemetry_buffer.drain_batch(redis, table="coaching_events")
    assert rows == []
    assert eids == []


@pytest.mark.asyncio
async def test_drain_batch_skips_malformed_entries() -> None:
    redis = MagicMock()
    redis.lpop = AsyncMock(
        return_value=[
            json.dumps({"row": ["good"], "event_id": "g"}),
            "not-json-at-all",
            json.dumps({"missing-row-key": True}),
        ]
    )
    rows, eids = await telemetry_buffer.drain_batch(redis, table="coaching_events")
    assert rows == [["good"]]
    assert eids == ["g"]


@pytest.mark.asyncio
async def test_queue_depth_returns_llen_value() -> None:
    redis = MagicMock()
    redis.llen = AsyncMock(return_value=7)
    out = await telemetry_buffer.queue_depth(redis, table="digital_events")
    assert out == 7
    redis.llen.assert_awaited_once_with("telemetry:retry:digital_events")
