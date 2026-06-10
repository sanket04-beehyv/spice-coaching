"""Drain Redis-backed ClickHouse retry queues after telemetry ingest outages."""

from __future__ import annotations

import logging

from platform_service.config import get_settings
from platform_service.deps import get_clickhouse_client, get_redis_client
from platform_service.services.telemetry_buffer import (
    COACHING_EVENTS_TABLE,
    drain_batch,
    enqueue_rows,
    queue_depth,
)

logger = logging.getLogger(__name__)


async def drain_telemetry_buffer_job(*, batch_size: int | None = None) -> dict[str, int]:
    """Retry buffered ClickHouse rows; re-enqueue on insert failure.

    Returns per-table stats for observability (drained, rebuffered, queue_depth).
    """
    settings = get_settings()
    size = batch_size or settings.telemetry_buffer_drain_batch_size
    ch_client = get_clickhouse_client()
    redis = get_redis_client()

    rows, event_ids = await drain_batch(redis, table=COACHING_EVENTS_TABLE, batch_size=size)
    if not rows:
        depth = await queue_depth(redis, table=COACHING_EVENTS_TABLE)
        if depth > 0:
            logger.info(
                "telemetry_buffer_drain: coaching_events queue_depth=%d (no batch popped)",
                depth,
            )
        return {"drained": 0, "rebuffered": 0, "queue_depth": depth}

    try:
        await ch_client.insert_coaching_events(rows)
        depth = await queue_depth(redis, table=COACHING_EVENTS_TABLE)
        logger.info(
            "telemetry_buffer_drain: inserted %d coaching_events row(s); queue_depth=%d",
            len(rows),
            depth,
        )
        return {"drained": len(rows), "rebuffered": 0, "queue_depth": depth}
    except Exception:
        logger.exception(
            "telemetry_buffer_drain: ClickHouse insert failed for %d row(s); re-buffering",
            len(rows),
        )
        await enqueue_rows(redis, table=COACHING_EVENTS_TABLE, rows=rows, event_ids=event_ids)
        depth = await queue_depth(redis, table=COACHING_EVENTS_TABLE)
        logger.warning(
            "telemetry_buffer_drain: rebuffered %d row(s); coaching_events queue_depth=%d",
            len(rows),
            depth,
        )
        return {"drained": 0, "rebuffered": len(rows), "queue_depth": depth}
