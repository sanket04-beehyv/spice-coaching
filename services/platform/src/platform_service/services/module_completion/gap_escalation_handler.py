"""Behavioural gap observation and quiz/SPICE outcome escalation."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.services.gap_state_service import GapStateService
from platform_service.services.module_completion.telemetry_parsing import spice_outcome_is_incorrect

logger = logging.getLogger(__name__)


class GapEscalationHandler:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def handle_quiz_attempt(
        self,
        *,
        chw_id: int,
        module: Module,
        score_pct: float | None,
        tenant_uuid: UUID | None,
        event_id: str | None,
        gap_outcome_kind: str | None,
    ) -> None:
        """Mirror quiz outcome on behavioural-gap state when the module declares a primary gap."""
        if module.primary_gap_id is None:
            return

        gap_svc = GapStateService(self._session)
        await gap_svc.record_observation(
            chw_id=chw_id,
            behavioural_gap_id=module.primary_gap_id,
            tenant_id=tenant_uuid,
            predicate=None,
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
            await gap_svc.record_failed_attempt(
                chw_id=chw_id,
                behavioural_gap_id=module.primary_gap_id,
                tenant_id=tenant_uuid,
            )
        elif gap_outcome_kind == "correct":
            await gap_svc.record_correct_quiz_attempt(
                chw_id=chw_id,
                behavioural_gap_id=module.primary_gap_id,
            )
        elif passed:
            await gap_svc.reset_after_pass(chw_id=chw_id, behavioural_gap_id=module.primary_gap_id)
        else:
            await gap_svc.record_failed_attempt(
                chw_id=chw_id,
                behavioural_gap_id=module.primary_gap_id,
                tenant_id=tenant_uuid,
            )

    async def handle_spice_action(
        self,
        *,
        chw_id: int,
        behavioural_gap_id: UUID,
        tenant_uuid: UUID | None,
        payload: dict[str, Any],
        payload_json: dict[str, Any],
        event_id: str | None,
    ) -> None:
        gap_svc = GapStateService(self._session)
        await gap_svc.record_observation(
            chw_id=chw_id,
            behavioural_gap_id=behavioural_gap_id,
            tenant_id=tenant_uuid,
            predicate=None,
        )
        if spice_outcome_is_incorrect(payload, payload_json):
            await gap_svc.record_failed_attempt(
                chw_id=chw_id,
                behavioural_gap_id=behavioural_gap_id,
                tenant_id=tenant_uuid,
            )
