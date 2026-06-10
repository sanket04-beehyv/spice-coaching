"""W-10 — telemetry event idempotency via Redis.

The SDK retries batches on network failure, app restart, and on a
delivery-uncertain ack. Without dedup we double-count every retried event
in ClickHouse and double-fire every gap-state update. This helper provides
a 24-hour Redis dedup window keyed by event_id.

Design notes:
- We use Redis SET NX EX (atomic set-if-not-exists with TTL). One round-trip
  per event_id; pipelining the whole batch is one round-trip total.
- 24-hour TTL is a defensive default — the SDK's retry windows are typically
  minutes to hours. Bumping to days only costs a few KB in Redis per million
  events and helps when a CHW comes back online after a long offline period.
- Keys are prefixed with `telemetry:event:` to keep the namespace clear from
  the existing scenarios pipeline keys.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from mc_contracts.telemetry import TelemetryEvent
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


_DEDUP_KEY_PREFIX = "telemetry:event:"
_DEDUP_TTL_SECONDS = 24 * 60 * 60  # 24 hours


def _key(event_id: str) -> str:
    return f"{_DEDUP_KEY_PREFIX}{event_id}"


async def is_first_seen(redis: Redis, event_id: str) -> bool:
    """True if this event_id was not previously seen (now claimed)."""
    # SET key value NX EX ttl — returns True iff the key was created.
    return bool(await redis.set(_key(event_id), "1", nx=True, ex=_DEDUP_TTL_SECONDS))


async def partition_for_dedup(
    redis: Redis,
    events: Iterable[TelemetryEvent],
) -> tuple[list[TelemetryEvent], list[str]]:
    """Split a batch into (first_seen, duplicate_ids).

    `first_seen` retains insertion order so the caller can preserve their
    own per-event metadata indices. Pipelined for one round-trip per call.
    """
    events_list = list(events)
    if not events_list:
        return [], []

    pipe = redis.pipeline()
    for e in events_list:
        pipe.set(_key(e.id), "1", nx=True, ex=_DEDUP_TTL_SECONDS)
    results = await pipe.execute()

    first_seen: list[TelemetryEvent] = []
    duplicate_ids: list[str] = []
    for event, was_set in zip(events_list, results, strict=True):
        # redis-py returns True if SET-NX created the key, None otherwise.
        if was_set:
            first_seen.append(event)
        else:
            duplicate_ids.append(event.id)

    if duplicate_ids:
        logger.info(
            "Telemetry dedup: dropped %d duplicate event(s); first_seen=%d",
            len(duplicate_ids),
            len(first_seen),
        )
    return first_seen, duplicate_ids
