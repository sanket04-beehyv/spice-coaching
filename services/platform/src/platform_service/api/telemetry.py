"""Telemetry ingest endpoint — receives SDK event batches.

POST /telemetry/events → TelemetryAckResponse

Routing:
- All events → ClickHouse `coaching_events` (including `event_family == digital`).
- `telemetry_behavioural_gap_state_enabled` (default false) selects operational state:
  - **false (quiz mode):** only `MODULE_QUIZ_ATTEMPTED` → `process_module_event_task`
    (per-quiz-question state in `chw_quiz_question_state`).
  - **true (gap mode):** `event_type` ∈ MODULE_* pipeline set (W-10 v3.3) →
    enqueue `process_module_event_task` (module completion + gap state); plus
    `event_type` == SPICE_ACTION_OBSERVED (gap observation from
    `payload_json.behavioural_gap_id`).
- `MODULE_REQUESTED` → enqueue `process_training_request_event_task` when
  `module_id` or non-empty `payload_json.requested_module_name` is present
  (independent of the gap-state flag; does not use the completion worker).

W-10 hardening (additive on top of the original handler):
1. Dedup by event_id via Redis SET-NX with 24h TTL — duplicates from SDK
   retries are dropped before write and reported in `duplicates`.
2. ClickHouse insert failures are caught and the rows are pushed to a
   Redis retry queue. The handler returns 202 with a `buffered` ack
   instead of 500 so the SDK doesn't keep retrying a doomed batch.
3. The ack response was widened (`duplicates`, `buffered` fields).
"""

from __future__ import annotations

import json
import logging
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from mc_contracts.enums import CoachingEventType
from mc_contracts.telemetry import TelemetryAckResponse, TelemetryBatch, TelemetryEvent

from platform_service.auth.spice_identity import require_chw_id_for_telemetry
from platform_service.celery_tasks import process_module_event_task, process_training_request_event_task
from platform_service.config import get_settings
from platform_service.deps import get_clickhouse_client, get_redis_client
from platform_service.services.telemetry_buffer import enqueue_rows
from platform_service.services.telemetry_dedup import partition_for_dedup

router = APIRouter(prefix="/telemetry", tags=["telemetry"])
logger = logging.getLogger(__name__)

GAP_PROFILE_QUEUE = "gap_profile_update_queue"


def _resolve_telemetry_chw_id(request: Request, batch: TelemetryBatch) -> int:
    return require_chw_id_for_telemetry(request, batch.chw_id)


# v3.3 module-pipeline event types (W-10). Anything in this set is routed
# to `process_module_event_task` instead of the scenario-level path.
# MODULE_REQUESTED is intentionally excluded (dedicated training-request task).
_MODULE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        CoachingEventType.MODULE_DELIVERED.value,
        CoachingEventType.MODULE_CARD_VIEWED.value,
        CoachingEventType.MODULE_QUIZ_ATTEMPTED.value,
    }
)


def _as_ch_value(v: object) -> object:
    """Convert Enum-like values to ClickHouse-safe primitives."""
    if v is None:
        return None
    value = getattr(v, "value", None)
    return value if value is not None else v


def _resolve_timestamp_utc(e: TelemetryEvent) -> int:
    """Return UTC epoch seconds for ClickHouse; fall back to local when omitted."""
    if e.timestamp_utc is not None:
        return e.timestamp_utc
    return e.timestamp_local


def _requested_module_name_from_payload(payload: dict | None) -> str | None:
    raw = (payload or {}).get("requested_module_name")
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return name or None


def _reason_from_payload(payload: dict | None) -> str | None:
    raw = (payload or {}).get("reason")
    if raw is None:
        return None
    if not isinstance(raw, str):
        return str(raw)
    return raw


def _event_to_row(
    *,
    e: TelemetryEvent,
    sdk_version: str,
    chw_id: int,
    tenant_id: UUID | None,
    synced_at_ms: int,
) -> list:
    """Convert TelemetryEvent to ClickHouse coaching_events column order.

    W-10 added three columns at the end (module_family_id, module_version,
    quiz_score_pct). Legacy events leave them NULL.
    """
    return [
        e.id,
        e.event_schema_version,
        sdk_version,
        e.session_id,
        e.patient_visit_id,
        e.patient_track_id,
        e.patient_id_hash,
        chw_id,
        tenant_id,
        e.village_id,
        e.upazila_id,
        e.event_family.value,
        _as_ch_value(e.event_type),
        e.module_family_id,
        e.module_id,
        e.card_family_id,
        e.quiz_id,
        e.module_version,
        e.quiz_score_pct,
        _as_ch_value(e.clinical_domain),
        _as_ch_value(e.card_type),
        _as_ch_value(e.trigger_type),
        _as_ch_value(e.inference_mode),
        _as_ch_value(e.outcome),
        _as_ch_value(e.validator_status),
        e.fallback_used,
        e.network_state,
        json.dumps(e.payload_json),
        e.event_date,
        _resolve_timestamp_utc(e),
        e.timestamp_local,
        synced_at_ms,
    ]


@router.post("/events", response_model=TelemetryAckResponse)
async def ingest_events(
    batch: TelemetryBatch,
    request: Request,
    chw_id: int = Depends(_resolve_telemetry_chw_id),
) -> TelemetryAckResponse:
    """Ingest a batch of telemetry events from the Android SDK."""
    accepted: list[str] = []
    rejected: list[str] = []
    errors: list[str] = []
    buffered: list[str] = []

    coaching_events: list[list] = []
    coaching_event_ids: list[str | None] = []
    # gap_jobs removed in the architecture reset (legacy scenario telemetry
    # path is gone); module_jobs is the completion/gap enqueue surface.
    # training_request_jobs handles MODULE_REQUESTED separately.
    module_jobs: list[dict] = []
    training_request_jobs: list[dict] = []

    synced_at_ms = int(time.time() * 1000)
    request_id = getattr(request.state, "request_id", None)
    _ch_client = get_clickhouse_client()
    gap_state_enabled = get_settings().telemetry_behavioural_gap_state_enabled

    # ── W-10 idempotency: drop duplicates BEFORE any side-effects ──
    redis = get_redis_client()
    first_seen, duplicate_ids = await partition_for_dedup(redis, batch.events)

    for event in first_seen:
        try:
            coaching_events.append(
                _event_to_row(
                    e=event,
                    sdk_version=batch.sdk_version,
                    chw_id=chw_id,
                    tenant_id=batch.tenant_id,
                    synced_at_ms=synced_at_ms,
                )
            )
            coaching_event_ids.append(event.id)
            accepted.append(event.id)

            event_type_value = _as_ch_value(event.event_type)

            if event_type_value == CoachingEventType.MODULE_REQUESTED.value:
                requested_name = _requested_module_name_from_payload(event.payload_json)
                if event.module_id is not None or requested_name:
                    training_job: dict = {
                        "chw_id": chw_id,
                        "tenant_id": batch.tenant_id,
                        "event_id": event.id,
                        "event_type": event_type_value,
                        "module_id": str(event.module_id) if event.module_id is not None else None,
                        "requested_module_name": requested_name,
                        "reason": _reason_from_payload(event.payload_json),
                        "payload_json": event.payload_json or {},
                    }
                    if request_id:
                        training_job["request_id"] = request_id
                    training_request_jobs.append(training_job)
                else:
                    logger.warning(
                        "module_requested missing module_id and requested_module_name event_id=%s",
                        event.id,
                    )
            elif gap_state_enabled:
                # W-10 module-pipeline path (gap mode). The legacy v3.0 scenario-level
                # gap-update path was deleted in the architecture reset (the
                # underlying scenario / chw_gap_profile tables are gone).
                if event_type_value in _MODULE_EVENT_TYPES and event.module_id is not None:
                    job: dict = {
                        "chw_id": chw_id,
                        "tenant_id": batch.tenant_id,
                        "event_id": event.id,
                        "event_type": event_type_value,
                        "module_id": str(event.module_id),
                        "quiz_id": str(event.quiz_id) if event.quiz_id is not None else None,
                        "quiz_score_pct": event.quiz_score_pct,
                        "outcome": _as_ch_value(event.outcome),
                        # Forward full payload for forward compatibility (e.g. per-question answers).
                        "payload_json": event.payload_json or {},
                    }
                    if request_id:
                        job["request_id"] = request_id
                    module_jobs.append(job)
                elif event_type_value == CoachingEventType.SPICE_ACTION_OBSERVED.value:
                    spice_job: dict = {
                        "chw_id": chw_id,
                        "tenant_id": batch.tenant_id,
                        "event_id": event.id,
                        "event_type": event_type_value,
                        "outcome": _as_ch_value(event.outcome),
                        "payload_json": event.payload_json or {},
                    }
                    if request_id:
                        spice_job["request_id"] = request_id
                    module_jobs.append(spice_job)
            elif (
                event_type_value == CoachingEventType.MODULE_QUIZ_ATTEMPTED.value
                and event.module_id is not None
            ):
                quiz_job: dict = {
                    "chw_id": chw_id,
                    "tenant_id": batch.tenant_id,
                    "event_id": event.id,
                    "event_type": event_type_value,
                    "module_id": str(event.module_id),
                    "quiz_id": str(event.quiz_id) if event.quiz_id is not None else None,
                    "quiz_score_pct": event.quiz_score_pct,
                    "outcome": _as_ch_value(event.outcome),
                    "payload_json": event.payload_json or {},
                }
                if request_id:
                    quiz_job["request_id"] = request_id
                module_jobs.append(quiz_job)
        except Exception as exc:
            rejected.append(event.id)
            errors.append(f"event_id={event.id}: {exc}")
            logger.warning("Telemetry event rejected event_id=%s: %s", event.id, exc)

    # ── ClickHouse writes with retry-buffer fallback (W-10) ──
    if coaching_events:
        await _insert_or_buffer(
            table="coaching_events",
            inserter=_ch_client.insert_coaching_events,
            rows=coaching_events,
            event_ids=coaching_event_ids,
            buffered_acc=buffered,
        )

    # Enqueue Celery jobs only for events that made it past validation. We
    # enqueue regardless of the ClickHouse outcome — the ClickHouse layer
    # is for analytics; operational state shouldn't be held hostage to a
    # ClickHouse outage.
    for j in module_jobs:
        process_module_event_task.delay(j)
    for j in training_request_jobs:
        process_training_request_event_task.delay(j)

    return TelemetryAckResponse(
        accepted=accepted,
        rejected=rejected,
        duplicates=duplicate_ids,
        buffered=buffered,
        errors=errors,
    )


async def _insert_or_buffer(
    *,
    table: str,
    inserter,
    rows: list[list],
    event_ids: list[str | None],
    buffered_acc: list[str],
) -> None:
    """Try inserting to ClickHouse; on failure, push rows to the Redis retry
    queue and record the affected event_ids in `buffered_acc`. Never raises."""
    try:
        await inserter(rows)
        return
    except Exception:
        logger.exception(
            "ClickHouse insert failed for %d %s row(s); buffering for retry",
            len(rows),
            table,
        )
    await enqueue_rows(get_redis_client(), table=table, rows=rows, event_ids=event_ids)
    buffered_acc.extend([eid for eid in event_ids if eid is not None])
