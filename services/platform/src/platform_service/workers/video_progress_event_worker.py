"""Process VIDEO_PROGRESS_UPDATED telemetry into chw_video_progress.

Enqueued from ``ingest_events``. Invalid / incomplete payloads are logged
no-ops so the SDK ACK remains best-effort.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from platform_service.db.base import SessionLocal
from platform_service.db.repositories.video_progress_repository import VideoProgressRepository
from platform_service.services.module_completion.telemetry_parsing import (
    coerce_tenant_uuid,
    parse_chw_id,
    parse_uuid,
)

logger = logging.getLogger(__name__)


def _payload_dict(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("payload_json")
    if isinstance(nested, dict):
        return nested
    return payload


def _parse_source_document_id(payload: dict[str, Any]) -> UUID | None:
    data = _payload_dict(payload)
    raw = data.get("source_document_id")
    if raw is None:
        raw = payload.get("source_document_id")
    if raw is None:
        return None
    return parse_uuid(raw, field="source_document_id")


def _parse_last_position_ms(payload: dict[str, Any]) -> int | None:
    data = _payload_dict(payload)
    raw = data.get("last_position_ms", payload.get("last_position_ms"))
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _parse_percent_watched(payload: dict[str, Any]) -> float | None:
    data = _payload_dict(payload)
    raw = data.get("percent_watched", payload.get("percent_watched"))
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0.0 or value > 100.0:
        return None
    return value


def _parse_completed(payload: dict[str, Any]) -> bool:
    data = _payload_dict(payload)
    raw = data.get("completed", payload.get("completed", False))
    if isinstance(raw, bool):
        return raw
    return False


async def process_video_progress_event_job(payload: dict[str, Any]) -> None:
    """Monotonically upsert video watch progress from one telemetry event.

    Expected keys (from ``api/telemetry.py``):
        chw_id, tenant_id, event_id, event_type, payload_json (with
        source_document_id, last_position_ms, percent_watched, completed).
    """
    event_id = payload.get("event_id")
    request_id = payload.get("request_id")
    if request_id:
        logger.info(
            "video_progress_event_worker event_id=%s request_id=%s",
            event_id,
            request_id,
        )

    chw_id = parse_chw_id(payload.get("chw_id"))
    if chw_id is None:
        logger.warning(
            "video_progress_event_worker dropping event_id=%s: missing chw_id",
            event_id,
        )
        return

    source_document_id = _parse_source_document_id(payload)
    if source_document_id is None:
        logger.warning(
            "video_progress_event_worker dropping event_id=%s: missing/invalid source_document_id",
            event_id,
        )
        return

    last_position_ms = _parse_last_position_ms(payload)
    if last_position_ms is None:
        logger.warning(
            "video_progress_event_worker dropping event_id=%s: missing/invalid last_position_ms",
            event_id,
        )
        return

    percent_watched = _parse_percent_watched(payload)
    if percent_watched is None:
        logger.warning(
            "video_progress_event_worker dropping event_id=%s: missing/invalid percent_watched",
            event_id,
        )
        return

    completed = _parse_completed(payload)
    tenant_id = coerce_tenant_uuid(payload.get("tenant_id"))

    async with SessionLocal() as session:
        row = await VideoProgressRepository(session).upsert(
            chw_id=chw_id,
            source_document_id=source_document_id,
            last_position_ms=last_position_ms,
            percent_watched=percent_watched,
            completed=completed,
            tenant_id=tenant_id,
        )
        await session.commit()
        logger.info(
            "video_progress_event_worker upserted event_id=%s chw_id=%s "
            "source_document_id=%s percent_watched=%s completed=%s",
            event_id,
            chw_id,
            source_document_id,
            row.percent_watched,
            row.completed,
        )
