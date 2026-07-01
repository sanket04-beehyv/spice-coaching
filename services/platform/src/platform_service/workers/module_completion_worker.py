"""W-10 — module completion worker.

Consumes the v3.3 module-pipeline telemetry events
(`MODULE_DELIVERED`, `MODULE_CARD_VIEWED`, `MODULE_QUIZ_ATTEMPTED`) and updates
`chw_module_completion` based on per-question quiz progress derived from
`MODULE_QUIZ_ATTEMPTED`, plus `chw_behavioural_gap_state` for
`MODULE_QUIZ_ATTEMPTED` (via the W-8 GapStateService).

Also consumes `SPICE_ACTION_OBSERVED` (clinical workflow hook): reads
`payload_json.behavioural_gap_id` and records a gap observation via
`GapStateService.record_observation`. When `outcome` (event top-level or
`payload_json.outcome`) is `wrong` or `incorrect`, also calls
`GapStateService.record_failed_attempt` to bump `failed_attempts_count`.

Score vs `settings.quiz_pass_threshold_default` (and per-module
`pass_threshold_override` when set) only drives gap updates when `outcome`
is absent. Per-question quiz progress (``chw_module_quiz_progress``) is
written on any ``MODULE_QUIZ_ATTEMPTED`` with a valid ``quiz_id``,
regardless of ``outcome``; module completion is stamped when every question
in the module has at least one attempt row.

For `chw_behavioural_gap_state`, `MODULE_QUIZ_ATTEMPTED` with a
``primary_gap_id`` calls ``record_observation`` first (``first_observed_at``,
``last_observed_at``, ``occurrence_count``), then applies quiz outcome logic.
``failed_attempts_count`` uses the event `outcome` when present:
``incorrect`` / ``wrong`` increments (via `record_failed_attempt`);
``correct`` decrements and sets ``status=resolved`` when the counter reaches
zero (inactive for gap-based suggestions). If `outcome` is absent or not one
of those values, gap updates fall back to the score-based pass/fail (same as
before).

Learning points for `MODULE_QUIZ_ATTEMPTED` are recorded only when `outcome`
is explicitly ``correct`` (top-level or `payload_json.outcome`); score-based
gap logic is unchanged when `outcome` is absent.

Reused infrastructure:
- `GapStateService.record_failed_attempt` — already implements escalation
  when failed_attempts_count crosses `settings.quiz_failure_escalation_count`
  within `settings.quiz_failure_escalation_window_days`.
- `GapStateService.reset_after_pass` — clears failed_attempts_count and sets
  `last_reinforced_at` so periodic refresh logic stays consistent.
- `GapStateService.record_observation` — SPICE and quiz paths (when module
  has ``primary_gap_id``); increments occurrence counters and observation
  timestamps on `chw_behavioural_gap_state`.
- `GapStateService.record_failed_attempt` — SPICE path when outcome is
  incorrect; same escalation semantics as quiz failures.
"""

from __future__ import annotations

import logging
from typing import Any

from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.repositories.gap_telemetry_repository import GapTelemetryRepository
from platform_service.services.module_completion import (
    GapEscalationHandler,
    LearningPointsHandler,
    QuizEscalationHandler,
    QuizProgressHandler,
    coerce_tenant_uuid,
    module_quiz_outcome_kind,
    parse_chw_id,
    parse_quiz_id,
    parse_uuid,
)

logger = logging.getLogger(__name__)


# Event types this worker handles. Matches mc_contracts.enums.CoachingEventType
# but kept as plain strings here so the worker doesn't fail on legacy SDK
# payloads that pass the value as a raw string.
_HANDLED = {
    "module_delivered",
    "module_card_viewed",
    "module_quiz_attempted",
    "spice_action_observed",
}


class ModuleNotFoundForEventError(Exception):
    """Permanent failure: telemetry references a module row that does not exist."""


async def _try_claim_module_event(session, payload: dict[str, Any]) -> bool:
    """Idempotent worker claim; duplicates return False (already processed)."""
    event_id = payload.get("event_id")
    chw_id = parse_chw_id(payload.get("chw_id"))
    if not event_id or chw_id is None:
        return False
    parsed = parse_uuid(event_id, field="event_id")
    if parsed is None:
        return False
    event_type = (payload.get("event_type") or "unknown").strip().lower()
    repo = GapTelemetryRepository(session)
    return await repo.try_claim_event(
        event_id=parsed,
        chw_id=chw_id,
        event_type=event_type,
        tenant_id=coerce_tenant_uuid(payload.get("tenant_id")),
    )


async def process_module_event_job(payload: dict[str, Any]) -> None:
    """Apply one module-level event to platform state.

    Expected `payload` keys (forwarded from api/telemetry.py):
        chw_id: int (JSON number or numeric string)
        tenant_id: int | None
        event_type: str
        event_id: str
        Module pipeline additionally: module_id (the specific Module row the
        SDK rendered — version is encoded in this id, so no separate version
        field is needed), quiz_id (``module_quiz_question.id`` for the question
        being answered), quiz_score_pct (0.0–1.0 on
        MODULE_QUIZ_ATTEMPTED; gap fallback only); optional outcome (correct |
        wrong | incorrect) drives gap failed_attempts_count when set.
        Per-question progress is recorded whenever ``quiz_id`` is present,
        regardless of ``outcome``. Learning points for MODULE_QUIZ_ATTEMPTED
        require outcome ``correct``.
        spice_action_observed additionally: payload_json with behavioural_gap_id;
        optional outcome on the job or in payload_json (`wrong` / `incorrect`
        increment failed_attempts_count via record_failed_attempt).
    """
    event_type = (payload.get("event_type") or "").strip().lower()
    request_id = payload.get("request_id")
    if request_id:
        logger.info(
            "module_completion_worker event_id=%s event_type=%s request_id=%s",
            payload.get("event_id"),
            event_type,
            request_id,
        )
    if event_type not in _HANDLED:
        logger.warning(
            "module_completion_worker received unhandled event_type=%s event_id=%s",
            event_type,
            payload.get("event_id"),
        )
        return

    if event_type in ("module_delivered", "module_card_viewed"):
        await _process_learning_points_only(payload, event_type=event_type)
        return

    if event_type == "spice_action_observed":
        if not get_settings().telemetry_behavioural_gap_state_enabled:
            logger.info(
                "module_completion_worker skipping spice_action_observed event_id=%s "
                "(telemetry_behavioural_gap_state_enabled=false)",
                payload.get("event_id"),
            )
            return
        await _process_spice_action(payload)
        return

    await _process_module_quiz(payload, event_type=event_type)


async def _process_learning_points_only(payload: dict[str, Any], *, event_type: str) -> None:
    chw_id = parse_chw_id(payload.get("chw_id"))
    if chw_id is None:
        logger.warning(
            "module_completion_worker dropping event_id=%s: missing chw_id for learning points",
            payload.get("event_id"),
        )
        return
    tenant_uuid = coerce_tenant_uuid(payload.get("tenant_id"))
    async with SessionLocal() as session:
        try:
            if not await _try_claim_module_event(session, payload):
                logger.info(
                    "module_completion_worker duplicate event_id=%s event_type=%s",
                    payload.get("event_id"),
                    event_type,
                )
                return
            await LearningPointsHandler(session).try_award_from_payload(
                event_id=payload.get("event_id"),
                chw_id=chw_id,
                tenant_id=tenant_uuid,
                event_type=event_type,
                payload=payload,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "module_completion_worker failed for event_id=%s event_type=%s",
                payload.get("event_id"),
                event_type,
            )
            raise


async def _process_spice_action(payload: dict[str, Any]) -> None:
    raw = payload.get("payload_json")
    payload_json: dict[str, Any] = raw if isinstance(raw, dict) else {}
    chw_id = parse_chw_id(payload.get("chw_id"))
    behavioural_gap_id = parse_uuid(
        payload_json.get("behavioural_gap_id"),
        field="behavioural_gap_id",
    )
    if chw_id is None or behavioural_gap_id is None:
        logger.warning(
            "module_completion_worker dropping event_id=%s spice_action_observed: "
            "missing or invalid chw_id or payload_json.behavioural_gap_id",
            payload.get("event_id"),
        )
        return
    tenant_uuid = coerce_tenant_uuid(payload.get("tenant_id"))
    async with SessionLocal() as session:
        try:
            if not await _try_claim_module_event(session, payload):
                logger.info(
                    "module_completion_worker duplicate event_id=%s event_type=%s",
                    payload.get("event_id"),
                    "spice_action_observed",
                )
                return
            await GapEscalationHandler(session).handle_spice_action(
                chw_id=chw_id,
                behavioural_gap_id=behavioural_gap_id,
                tenant_uuid=tenant_uuid,
                payload=payload,
                payload_json=payload_json,
                event_id=payload.get("event_id"),
            )
            await LearningPointsHandler(session).try_award_from_payload(
                event_id=payload.get("event_id"),
                chw_id=chw_id,
                tenant_id=tenant_uuid,
                event_type="spice_action_observed",
                payload=payload,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "module_completion_worker failed for event_id=%s event_type=%s",
                payload.get("event_id"),
                "spice_action_observed",
            )
            raise


async def _process_module_quiz(payload: dict[str, Any], *, event_type: str) -> None:
    chw_id = parse_chw_id(payload.get("chw_id"))
    module_id = parse_uuid(payload.get("module_id"), field="module_id")
    if chw_id is None or module_id is None:
        logger.warning(
            "module_completion_worker dropping event_id=%s: missing chw_id or module_id",
            payload.get("event_id"),
        )
        return

    tenant_uuid = coerce_tenant_uuid(payload.get("tenant_id"))

    async with SessionLocal() as session:
        try:
            if not await _try_claim_module_event(session, payload):
                logger.info(
                    "module_completion_worker duplicate event_id=%s event_type=%s",
                    payload.get("event_id"),
                    event_type,
                )
                return
            module = await session.get(Module, module_id)
            if module is None:
                logger.error(
                    "module_completion_worker: no module row for module_id=%s event_id=%s",
                    module_id,
                    payload.get("event_id"),
                )
                raise ModuleNotFoundForEventError(
                    f"module_id={module_id} not found for event_id={payload.get('event_id')}"
                )

            quiz_id = parse_quiz_id(payload)
            if quiz_id is not None:
                await QuizProgressHandler(session).record_question_attempted_and_maybe_complete(
                    chw_id=chw_id,
                    tenant_uuid=tenant_uuid,
                    module=module,
                    quiz_id=quiz_id,
                )

            if event_type == "module_quiz_attempted":
                if get_settings().telemetry_behavioural_gap_state_enabled:
                    await GapEscalationHandler(session).handle_quiz_attempt(
                        chw_id=chw_id,
                        module=module,
                        score_pct=payload.get("quiz_score_pct"),
                        tenant_uuid=tenant_uuid,
                        event_id=payload.get("event_id"),
                        gap_outcome_kind=module_quiz_outcome_kind(payload),
                    )
                elif quiz_id is not None:
                    await QuizEscalationHandler(session).handle_quiz_attempt(
                        chw_id=chw_id,
                        module=module,
                        quiz_id=quiz_id,
                        score_pct=payload.get("quiz_score_pct"),
                        tenant_uuid=tenant_uuid,
                        event_id=payload.get("event_id"),
                        gap_outcome_kind=module_quiz_outcome_kind(payload),
                    )
                else:
                    logger.warning(
                        "module_completion_worker dropping quiz escalation event_id=%s: "
                        "missing quiz_id in quiz-id telemetry mode",
                        payload.get("event_id"),
                    )

            await LearningPointsHandler(session).try_award_from_payload(
                event_id=payload.get("event_id"),
                chw_id=chw_id,
                tenant_id=tenant_uuid,
                event_type=event_type,
                payload=payload,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception(
                "module_completion_worker failed for event_id=%s event_type=%s",
                payload.get("event_id"),
                event_type,
            )
            raise
