"""Award learning points from telemetry (idempotent per event id)."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.config_threshold_repository import ConfigThresholdRepository
from platform_service.db.repositories.learning_points_repository import LearningPointsRepository
from platform_service.services.learning_points_thresholds import (
    LEARNING_POINTS_THRESHOLD_DEFAULTS,
    learning_points_delta_for_event,
)

logger = logging.getLogger(__name__)


class LearningPointsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_award_from_telemetry(
        self,
        *,
        event_id: object | None,
        chw_id: int,
        tenant_id: uuid.UUID | None,
        event_type: str,
        quiz_score_pct: float | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Parse `event_id`, compute delta from `config_threshold`, insert one scoring row if new."""
        if event_id is None:
            return False
        try:
            eid = uuid.UUID(str(event_id).strip())
        except (ValueError, TypeError, AttributeError):
            logger.warning("learning_points: invalid event_id=%r", event_id)
            return False

        ctr = ConfigThresholdRepository(self._session)
        thresholds = await ctr.get_int_for_keys(LEARNING_POINTS_THRESHOLD_DEFAULTS)
        delta = learning_points_delta_for_event(
            event_type,
            quiz_score_pct=quiz_score_pct,
            thresholds=thresholds,
        )
        if delta <= 0:
            return False

        repo = LearningPointsRepository(self._session)
        return await repo.try_claim_and_increment(
            event_id=eid,
            chw_id=chw_id,
            tenant_id=tenant_id,
            delta=delta,
            now=now,
        )

    async def get_total_points(self, *, chw_id: int) -> int:
        repo = LearningPointsRepository(self._session)
        return await repo.get_total_points(chw_id=chw_id)
