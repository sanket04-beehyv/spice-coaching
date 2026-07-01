"""Per-CHW quiz-question state service (quiz-id telemetry mode).

Mirrors the quiz-outcome write path of ``GapStateService`` but keyed by
``(chw_id, quiz_id)`` instead of behavioural gap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.chw_quiz_question_state import CHWQuizQuestionState


def _now() -> datetime:
    return datetime.now(UTC)


class QuizQuestionStateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get_state_for_update(
        self,
        *,
        chw_id: int,
        quiz_id: UUID,
    ) -> CHWQuizQuestionState | None:
        result = await self._session.execute(
            select(CHWQuizQuestionState)
            .where(
                CHWQuizQuestionState.chw_id == chw_id,
                CHWQuizQuestionState.quiz_id == quiz_id,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def record_attempt(
        self,
        *,
        chw_id: int,
        quiz_id: UUID,
        module_id: UUID,
        tenant_id: UUID | None = None,
        now: datetime | None = None,
    ) -> CHWQuizQuestionState:
        """Record one quiz attempt observation (first/last attempt timestamps)."""
        ts = now or _now()
        state = await self._get_state_for_update(chw_id=chw_id, quiz_id=quiz_id)
        if state is None:
            state = CHWQuizQuestionState(
                chw_id=chw_id,
                quiz_id=quiz_id,
                module_id=module_id,
                tenant_id=tenant_id,
                first_attempt_at=ts,
                last_attempt_at=ts,
                updated_at=ts,
                status="active",
            )
            self._session.add(state)
            await self._session.flush()
        else:
            state.last_attempt_at = ts
            if state.first_attempt_at is None:
                state.first_attempt_at = ts
            state.updated_at = ts
            await self._session.flush()
        return state

    async def record_failed_attempt(
        self,
        *,
        chw_id: int,
        quiz_id: UUID,
        module_id: UUID,
        tenant_id: UUID | None = None,
        now: datetime | None = None,
    ) -> CHWQuizQuestionState:
        """Record a failed quiz attempt and escalate when threshold crossed."""
        ts = now or _now()
        settings = get_settings()
        state = await self._get_state_for_update(chw_id=chw_id, quiz_id=quiz_id)
        if state is None:
            state = CHWQuizQuestionState(
                chw_id=chw_id,
                quiz_id=quiz_id,
                module_id=module_id,
                tenant_id=tenant_id,
                failed_attempts_count=1,
                last_failed_attempt_at=ts,
                first_attempt_at=ts,
                last_attempt_at=ts,
                updated_at=ts,
                status="active",
            )
            self._session.add(state)
            await self._session.flush()
        else:
            window = timedelta(days=settings.quiz_failure_escalation_window_days)
            if state.last_failed_attempt_at is not None and ts - state.last_failed_attempt_at > window:
                state.failed_attempts_count = 1
            else:
                state.failed_attempts_count += 1
            state.last_failed_attempt_at = ts
            state.last_attempt_at = ts
            if state.first_attempt_at is None:
                state.first_attempt_at = ts
            state.updated_at = ts
            state.status = "active"
            await self._session.flush()

        if state.failed_attempts_count >= settings.quiz_failure_escalation_count:
            state.escalated_to_supervisor = True
            await self._session.flush()
        return state

    async def record_correct_attempt(
        self,
        *,
        chw_id: int,
        quiz_id: UUID,
        now: datetime | None = None,
    ) -> CHWQuizQuestionState | None:
        """Reset failed_attempts_count to zero when positive (or negative).

        A correct outcome clears accumulated failures, de-escalates, and
        resolves the row. When the counter is already zero, only ``updated_at``
        is touched.
        """
        ts = now or _now()
        state = await self._get_state_for_update(chw_id=chw_id, quiz_id=quiz_id)
        if state is None:
            return None
        prev = state.failed_attempts_count
        if prev != 0:
            state.failed_attempts_count = 0
            state.escalated_to_supervisor = False
            state.status = "resolved"
            state.last_failed_attempt_at = None
        state.updated_at = ts
        await self._session.flush()
        return state

    async def reset_after_pass(
        self,
        *,
        chw_id: int,
        quiz_id: UUID,
        now: datetime | None = None,
    ) -> CHWQuizQuestionState | None:
        """Clear failed-attempts counter and de-escalate after a passed quiz."""
        ts = now or _now()
        state = await self._get_state_for_update(chw_id=chw_id, quiz_id=quiz_id)
        if state is None:
            return None
        state.failed_attempts_count = 0
        state.escalated_to_supervisor = False
        state.status = "resolved"
        state.last_failed_attempt_at = None
        state.updated_at = ts
        await self._session.flush()
        return state
