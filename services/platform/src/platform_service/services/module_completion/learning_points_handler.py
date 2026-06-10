"""Learning points side effects from module telemetry events."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.services.learning_points_service import LearningPointsService
from platform_service.services.module_completion.telemetry_parsing import (
    module_quiz_outcome_kind,
    parse_quiz_score_pct,
)


class LearningPointsHandler:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_award_from_payload(
        self,
        *,
        event_id: str | None,
        chw_id: int,
        tenant_id: UUID | None,
        event_type: str,
        payload: dict,
    ) -> None:
        """Award learning points when event type and outcome rules allow it."""
        pts = LearningPointsService(self._session)
        quiz_pct = parse_quiz_score_pct(payload.get("quiz_score_pct"))
        award_quiz_points = (
            event_type != "module_quiz_attempted" or module_quiz_outcome_kind(payload) == "correct"
        )
        if not award_quiz_points:
            return
        await pts.try_award_from_telemetry(
            event_id=event_id,
            chw_id=chw_id,
            tenant_id=tenant_id,
            event_type=event_type,
            quiz_score_pct=quiz_pct if event_type == "module_quiz_attempted" else None,
        )
