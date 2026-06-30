"""Quiz-question outcome escalation (quiz-id telemetry mode)."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.services.quiz_question_state_service import QuizQuestionStateService

logger = logging.getLogger(__name__)


class QuizEscalationHandler:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def handle_quiz_attempt(
        self,
        *,
        chw_id: int,
        module: Module,
        quiz_id: UUID,
        score_pct: float | None,
        tenant_uuid: UUID | None,
        event_id: str | None,
        gap_outcome_kind: str | None,
    ) -> None:
        """Mirror quiz outcome on per-question state keyed by quiz_id."""
        quiz_svc = QuizQuestionStateService(self._session)
        await quiz_svc.record_attempt(
            chw_id=chw_id,
            quiz_id=quiz_id,
            module_id=module.id,
            tenant_id=tenant_uuid,
        )

        if score_pct is None:
            logger.warning(
                "module_completion: quiz_score_pct missing on event_id=%s; treating as 0.0/fail",
                event_id,
            )
            score_pct = 0.0
        score_pct = max(0.0, min(1.0, float(score_pct)))

        settings = get_settings()
        threshold = (
            module.pass_threshold_override
            if getattr(module, "pass_threshold_override", None) is not None
            else settings.quiz_pass_threshold_default
        )
        passed = score_pct >= threshold

        if gap_outcome_kind == "incorrect":
            await quiz_svc.record_failed_attempt(
                chw_id=chw_id,
                quiz_id=quiz_id,
                module_id=module.id,
                tenant_id=tenant_uuid,
            )
        elif gap_outcome_kind == "correct":
            await quiz_svc.record_correct_attempt(chw_id=chw_id, quiz_id=quiz_id)
        elif passed:
            await quiz_svc.reset_after_pass(chw_id=chw_id, quiz_id=quiz_id)
        else:
            await quiz_svc.record_failed_attempt(
                chw_id=chw_id,
                quiz_id=quiz_id,
                module_id=module.id,
                tenant_id=tenant_uuid,
            )
