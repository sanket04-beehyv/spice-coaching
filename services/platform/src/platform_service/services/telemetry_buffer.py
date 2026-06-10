"""W-10 — Redis-backed retry buffer for ClickHouse insert failures.

When ClickHouse is unreachable (network blip, restart, capacity), the
existing telemetry handler raises and the SDK sees a 500 — costing the
batch entirely. This buffer pushes the un-inserted rows onto a Redis list,
the handler returns 202 with a `buffered` ack, and a drain worker
(`workers/telemetry_buffer_drain.py`) periodically retries.

Retry lists are keyed by ClickHouse table name, e.g.:
    telemetry:retry:coaching_events

Each list element is a JSON-encoded {"row": [...], "event_id": str|None}.
We track event_id alongside the row so the drain worker can re-dedup
through the same path as live ingest.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


def _json_default(value: Any) -> Any:
    """Serialise types that show up in ClickHouse row tuples but aren't JSON-native:
    date/datetime → ISO string, UUID → str."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


COACHING_EVENTS_TABLE = "coaching_events"

_QUEUE_PREFIX = "telemetry:retry:"
# Cap the total backlog so a long ClickHouse outage doesn't fill Redis.
# Pilot scale: ~5K events/day. 50K is a 10-day buffer — plenty.
_MAX_QUEUE_LENGTH = 50_000


def _queue_key(table: str) -> str:
    return f"{_QUEUE_PREFIX}{table}"


async def enqueue_rows(
    redis: Redis,
    *,
    table: str,
    rows: list[list[Any]],
    event_ids: list[str | None] | None = None,
) -> int:
    """Push rows onto the retry queue. Returns the count actually enqueued.

    If `event_ids` is provided it must align 1:1 with `rows`; otherwise the
    rows are stored without per-row event ids (drain worker won't dedup).
    """
    if not rows:
        return 0
    if event_ids is None:
        event_ids = [None] * len(rows)
    if len(event_ids) != len(rows):
        raise ValueError("event_ids length must match rows length")

    payloads = [
        json.dumps({"row": row, "event_id": eid}, default=_json_default)
        for row, eid in zip(rows, event_ids, strict=True)
    ]
    key = _queue_key(table)
    pipe = redis.pipeline()
    pipe.rpush(key, *payloads)
    pipe.ltrim(key, -_MAX_QUEUE_LENGTH, -1)  # keep the most recent N
    pipe.llen(key)
    _, _, current_len = await pipe.execute()
    logger.warning(
        "Telemetry retry: buffered %d row(s) on %s queue (depth now=%d)",
        len(rows),
        table,
        current_len,
    )
    return len(rows)


async def queue_depth(redis: Redis, *, table: str) -> int:
    """Return current depth of the retry queue (for observability/dashboards)."""
    return int(await redis.llen(_queue_key(table)))


async def drain_batch(
    redis: Redis,
    *,
    table: str,
    batch_size: int = 100,
) -> tuple[list[list[Any]], list[str | None]]:
    """Pop up to `batch_size` rows for retry; returns (rows, event_ids).

    Uses LPOP-with-count for atomicity — caller is responsible for
    re-enqueueing if the downstream insert fails again.
    """
    raw = await redis.lpop(_queue_key(table), batch_size)
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        # Older redis-py returns a single str when count is omitted; we always
        # pass count, so this path is just defensive.
        raw = [raw]
    rows: list[list[Any]] = []
    event_ids: list[str | None] = []
    for item in raw:
        try:
            payload = json.loads(item)
            rows.append(payload["row"])
            event_ids.append(payload.get("event_id"))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Skipping malformed retry-queue entry: %s — %s", item[:80], exc)
    return rows, event_ids
