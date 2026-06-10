"""W-8 — Per-CHW gap state service (v3.3 path).

Sits alongside the legacy `GapProfileService` but operates on the v3.3
table `chw_behavioural_gap_state`. Each telemetry observation of a
behavioural-gap pattern increments occurrence_count (or resets it to 1 if
the previous observation is outside the trigger's window).

The supervisor escalation flag (`escalated_to_supervisor`) is set when
`failed_attempts_count` exceeds the configured threshold within the
configured window — Architecture R8 / Pipeline §12A.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.chw_behavioural_gap_state import CHWBehaviouralGapState
from platform_service.services.trigger_evaluator import (
    GapTriggerDecision,
    evaluate_gap_trigger,
    should_reset_window,
)


@dataclass(frozen=True)
class ObservationOutcome:
    """Result of recording one telemetry observation."""

    state: CHWBehaviouralGapState
    decision: GapTriggerDecision | None  # None when no predicate evaluated


def _now() -> datetime:
    return datetime.now(UTC)


class GapStateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Read ───────────────────────────────────────────────────────────

    async def get_state(self, *, chw_id: int, behavioural_gap_id: UUID) -> CHWBehaviouralGapState | None:
        result = await self._session.execute(
            select(CHWBehaviouralGapState).where(
                CHWBehaviouralGapState.chw_id == chw_id,
                CHWBehaviouralGapState.behavioural_gap_id == behavioural_gap_id,
            )
        )
        return result.scalar_one_or_none()

    async def _get_state_for_update(
        self,
        *,
        chw_id: int,
        behavioural_gap_id: UUID,
    ) -> CHWBehaviouralGapState | None:
        result = await self._session.execute(
            select(CHWBehaviouralGapState)
            .where(
                CHWBehaviouralGapState.chw_id == chw_id,
                CHWBehaviouralGapState.behavioural_gap_id == behavioural_gap_id,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_gap_by_code(self, gap_code: str) -> BehaviouralGap | None:
        result = await self._session.execute(
            select(BehaviouralGap).where(BehaviouralGap.gap_code == gap_code)
        )
        return result.scalar_one_or_none()

    # ── Write ──────────────────────────────────────────────────────────

    async def record_observation(
        self,
        *,
        chw_id: int,
        behavioural_gap_id: UUID,
        predicate: dict[str, Any] | None = None,
        tenant_id: UUID | None = None,
        now: datetime | None = None,
    ) -> ObservationOutcome:
        """Record one telemetry observation of the gap pattern.

        - Creates the state row if it doesn't exist.
        - Increments occurrence_count (or resets to 1 if outside window).
        - Updates last_observed_at (and first_observed_at if missing).
        - Returns the trigger decision against the supplied predicate (if any).
        """
        ts = now or _now()
        state = await self._get_state_for_update(chw_id=chw_id, behavioural_gap_id=behavioural_gap_id)
        if state is None:
            state = CHWBehaviouralGapState(
                chw_id=chw_id,
                behavioural_gap_id=behavioural_gap_id,
                tenant_id=tenant_id,
                occurrence_count=1,
                first_observed_at=ts,
                last_observed_at=ts,
                updated_at=ts,
                status="active",
            )
            self._session.add(state)
            await self._session.flush()
        else:
            # Window-expired counter reset before we increment.
            if predicate is not None and should_reset_window(state, predicate, now=ts):
                state.occurrence_count = 1
            else:
                state.occurrence_count += 1
            state.last_observed_at = ts
            if state.first_observed_at is None:
                state.first_observed_at = ts
            state.updated_at = ts
            await self._session.flush()

        decision = evaluate_gap_trigger(state, predicate, now=ts) if predicate is not None else None
        return ObservationOutcome(state=state, decision=decision)

    async def record_failed_attempt(
        self,
        *,
        chw_id: int,
        behavioural_gap_id: UUID,
        tenant_id: UUID | None = None,
        now: datetime | None = None,
    ) -> CHWBehaviouralGapState:
        """Record a failed quiz attempt and (if threshold crossed within
        window) flip escalated_to_supervisor=True."""
        ts = now or _now()
        settings = get_settings()
        state = await self._get_state_for_update(chw_id=chw_id, behavioural_gap_id=behavioural_gap_id)
        if state is None:
            state = CHWBehaviouralGapState(
                chw_id=chw_id,
                behavioural_gap_id=behavioural_gap_id,
                tenant_id=tenant_id,
                failed_attempts_count=1,
                last_failed_attempt_at=ts,
                updated_at=ts,
                status="active",
            )
            self._session.add(state)
            await self._session.flush()
        else:
            window = timedelta(days=settings.quiz_failure_escalation_window_days)
            if state.last_failed_attempt_at is not None and ts - state.last_failed_attempt_at > window:
                # Outside escalation window — reset counter to this attempt.
                state.failed_attempts_count = 1
            else:
                state.failed_attempts_count += 1
            state.last_failed_attempt_at = ts
            state.updated_at = ts
            state.status = "active"
            await self._session.flush()

        if state.failed_attempts_count >= settings.quiz_failure_escalation_count:
            state.escalated_to_supervisor = True
            await self._session.flush()
        return state

    async def record_correct_quiz_attempt(
        self,
        *,
        chw_id: int,
        behavioural_gap_id: UUID,
        now: datetime | None = None,
    ) -> CHWBehaviouralGapState | None:
        """Apply one correct quiz outcome: decrement failed_attempts_count.

        When the counter drops to zero, set status to ``resolved`` so the gap
        no longer appears in active/monitoring module suggestions.
        """
        ts = now or _now()
        settings = get_settings()
        state = await self._get_state_for_update(chw_id=chw_id, behavioural_gap_id=behavioural_gap_id)
        if state is None:
            return None
        prev = state.failed_attempts_count
        if prev > 0:
            state.failed_attempts_count = prev - 1
        if state.failed_attempts_count < settings.quiz_failure_escalation_count:
            state.escalated_to_supervisor = False
        if prev > 0 and state.failed_attempts_count == 0:
            state.status = "resolved"
            state.last_failed_attempt_at = None
        state.updated_at = ts
        await self._session.flush()
        return state

    async def reset_after_pass(
        self,
        *,
        chw_id: int,
        behavioural_gap_id: UUID,
        now: datetime | None = None,
    ) -> CHWBehaviouralGapState | None:
        """Clear the failed-attempts counter and de-escalate after a passed quiz."""
        ts = now or _now()
        state = await self._get_state_for_update(chw_id=chw_id, behavioural_gap_id=behavioural_gap_id)
        if state is None:
            return None
        state.failed_attempts_count = 0
        state.escalated_to_supervisor = False
        state.last_reinforced_at = ts
        state.updated_at = ts
        await self._session.flush()
        return state
