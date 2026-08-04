"""Process MODULE_REQUESTED telemetry into CHW training requests.

Enqueued from ``ingest_events`` (dedicated task — not the module completion
worker). Calls ``TrainingRequestService.submit``; invalid modules and
duplicates are logged and no-oped so the SDK ACK remains best-effort.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from platform_service.db.base import SessionLocal
from platform_service.services.module_completion.telemetry_parsing import (
    coerce_tenant_uuid,
    parse_chw_id,
    parse_uuid,
)
from platform_service.services.training_request_service import (
    DuplicateTrainingRequestError,
    InvalidModuleError,
    TrainingRequestService,
)

logger = logging.getLogger(__name__)


def _parse_requested_module_name(payload: dict[str, Any]) -> str | None:
    raw = payload.get("requested_module_name")
    if raw is None:
        nested = payload.get("payload_json")
        if isinstance(nested, dict):
            raw = nested.get("requested_module_name")
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return name or None


def _parse_reason(payload: dict[str, Any]) -> str | None:
    raw = payload.get("reason")
    if raw is None:
        nested = payload.get("payload_json")
        if isinstance(nested, dict):
            raw = nested.get("reason")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return str(raw)


async def process_training_request_event_job(payload: dict[str, Any]) -> None:
    """Create a training request (and assignment when applicable) from one event.

    Expected keys (from ``api/telemetry.py``):
        chw_id, tenant_id, event_id, event_type, module_id (optional),
        requested_module_name (optional), reason (optional).
    """
    event_id = payload.get("event_id")
    request_id = payload.get("request_id")
    if request_id:
        logger.info(
            "training_request_event_worker event_id=%s request_id=%s",
            event_id,
            request_id,
        )

    chw_id = parse_chw_id(payload.get("chw_id"))
    if chw_id is None:
        logger.warning(
            "training_request_event_worker dropping event_id=%s: missing chw_id",
            event_id,
        )
        return

    module_id: UUID | None = None
    raw_module_id = payload.get("module_id")
    if raw_module_id is not None:
        module_id = parse_uuid(raw_module_id, field="module_id")
        if module_id is None:
            logger.warning(
                "training_request_event_worker dropping event_id=%s: invalid module_id=%r",
                event_id,
                raw_module_id,
            )
            return

    requested_name = _parse_requested_module_name(payload)
    if module_id is None and not requested_name:
        logger.warning(
            "training_request_event_worker dropping event_id=%s: missing module_id and requested_module_name",
            event_id,
        )
        return

    reason = _parse_reason(payload)
    tenant_id = coerce_tenant_uuid(payload.get("tenant_id"))

    async with SessionLocal() as session:
        service = TrainingRequestService(session)
        try:
            result = await service.submit(
                chw_id=chw_id,
                module_id=module_id,
                requested_module_name=requested_name,
                reason=reason,
                tenant_id=tenant_id,
            )
            await session.commit()
            logger.info(
                "training_request_event_worker created request_id=%s event_id=%s module_id=%s chw_id=%s",
                result.request_id,
                event_id,
                result.module_id,
                chw_id,
            )
        except InvalidModuleError:
            await session.rollback()
            logger.info(
                "training_request_event_worker no-op invalid_module event_id=%s module_id=%s chw_id=%s",
                event_id,
                module_id,
                chw_id,
            )
        except DuplicateTrainingRequestError:
            await session.rollback()
            logger.info(
                "training_request_event_worker no-op duplicate event_id=%s "
                "module_id=%s requested_module_name=%s chw_id=%s",
                event_id,
                module_id,
                requested_name,
                chw_id,
            )
