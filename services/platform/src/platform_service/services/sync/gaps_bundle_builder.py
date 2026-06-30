"""Build gaps sync bundles for device sync."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from mc_contracts.sync import (
    BehaviouralGapSyncPayload,
    CHWBehaviouralGapStateSyncPayload,
    CHWModuleCompletionSyncPayload,
    CHWModulePartialCompletionSyncPayload,
    CHWQuizQuestionStateSyncPayload,
    GapsSyncBundle,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.repositories.behavioural_gap_repository import BehaviouralGapRepository
from platform_service.db.repositories.chw_sync_repository import CHWSyncRepository
from platform_service.db.repositories.module_completion_repository import ModuleCompletionRepository
from platform_service.services.learning_points_service import LearningPointsService


class GapsBundleBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(
        self,
        *,
        since: datetime | None,
        chw_id: int | None,
        tenant_id: UUID | None = None,
    ) -> GapsSyncBundle:
        gaps = await BehaviouralGapRepository(self._session).list_active_updated_since(
            since,
            tenant_id=tenant_id,
        )
        gap_payloads = [
            BehaviouralGapSyncPayload(
                id=gap.id,
                gap_code=gap.gap_code,
                description=gap.description,
                domain=gap.domain,
                severity_default=gap.severity_default,
                detection_rule_jsonb=dict(gap.detection_rule_jsonb or {}),
                updated_at=gap.updated_at,
            )
            for gap in gaps
        ]

        if chw_id is None:
            return GapsSyncBundle(
                behavioural_gaps=gap_payloads,
                chw_behavioural_gap_states=[],
                chw_quiz_question_states=[],
                chw_module_completions=[],
                chw_module_partial_completions=[],
                server_time_utc=datetime.now(UTC).isoformat(),
                total_points=0,
            )

        chw_sync_repo = CHWSyncRepository(self._session)
        gap_state_enabled = get_settings().telemetry_behavioural_gap_state_enabled

        if gap_state_enabled:
            states = await chw_sync_repo.list_gap_states_for_chw(chw_id=chw_id, since=since)
            state_payloads = [
                CHWBehaviouralGapStateSyncPayload(
                    chw_id=state.chw_id,
                    behavioural_gap_id=state.behavioural_gap_id,
                    tenant_id=state.tenant_id,
                    severity_current=state.severity_current,
                    first_observed_at=state.first_observed_at,
                    last_observed_at=state.last_observed_at,
                    last_reinforced_at=state.last_reinforced_at,
                    occurrence_count=state.occurrence_count,
                    failed_attempts_count=state.failed_attempts_count,
                    last_failed_attempt_at=state.last_failed_attempt_at,
                    escalated_to_supervisor=state.escalated_to_supervisor,
                    status=state.status,
                    updated_at=state.updated_at,
                )
                for state in states
            ]
            quiz_state_payloads: list[CHWQuizQuestionStateSyncPayload] = []
        else:
            state_payloads = []
            quiz_rows = await chw_sync_repo.list_quiz_question_states_for_chw(chw_id=chw_id, since=since)
            quiz_state_payloads = [
                CHWQuizQuestionStateSyncPayload(
                    chw_id=row.chw_id,
                    quiz_id=row.quiz_id,
                    module_id=row.module_id,
                    tenant_id=row.tenant_id,
                    failed_attempts_count=row.failed_attempts_count,
                    last_failed_attempt_at=row.last_failed_attempt_at,
                    first_attempt_at=row.first_attempt_at,
                    last_attempt_at=row.last_attempt_at,
                    escalated_to_supervisor=row.escalated_to_supervisor,
                    status=row.status,
                    updated_at=row.updated_at,
                )
                for row in quiz_rows
            ]

        comps = await ModuleCompletionRepository(self._session).list_for_chw_updated_since(
            chw_id=chw_id,
            since=since,
        )
        comp_payloads = [
            CHWModuleCompletionSyncPayload(
                chw_id=comp.chw_id,
                module_family_id=comp.module_family_id,
                latest_completed_module_id=comp.latest_completed_module_id,
                latest_attempt_module_id=comp.latest_attempt_module_id,
                completed_at=comp.completed_at,
                latest_attempt_at=comp.latest_attempt_at,
                latest_quiz_score=comp.latest_quiz_score,
                latest_attempt_passed=comp.latest_attempt_passed,
                attempts_since_last_pass=comp.attempts_since_last_pass,
                reinforcement_due_at=comp.reinforcement_due_at,
                tenant_id=comp.tenant_id,
            )
            for comp in comps
        ]

        partial_payloads = await self._build_partial_completion_payloads(
            chw_id=chw_id,
            since=since,
        )
        total_pts = await LearningPointsService(self._session).get_total_points(chw_id=chw_id)

        return GapsSyncBundle(
            behavioural_gaps=gap_payloads,
            chw_behavioural_gap_states=state_payloads,
            chw_quiz_question_states=quiz_state_payloads,
            chw_module_completions=comp_payloads,
            chw_module_partial_completions=partial_payloads,
            server_time_utc=datetime.now(UTC).isoformat(),
            total_points=total_pts,
        )

    async def _build_partial_completion_payloads(
        self,
        *,
        chw_id: int,
        since: datetime | None,
    ) -> list[CHWModulePartialCompletionSyncPayload]:
        """Modules with quiz progress rows and at least one unanswered question."""
        chw_sync_repo = CHWSyncRepository(self._session)
        module_ids = await chw_sync_repo.list_module_ids_with_quiz_progress(
            chw_id=chw_id,
            since=since,
        )
        if not module_ids:
            return []

        tenant_by_module = await chw_sync_repo.tenant_id_by_module_for_chw(
            chw_id=chw_id,
            module_ids=module_ids,
        )
        incomplete_rows = await chw_sync_repo.list_incomplete_quiz_rows(
            chw_id=chw_id,
            module_ids=module_ids,
        )

        incomplete_by_module: dict[UUID, list[UUID]] = {}
        family_by_module: dict[UUID, UUID] = {}
        for module_id, quiz_id, module_family_id in incomplete_rows:
            incomplete_by_module.setdefault(module_id, []).append(quiz_id)
            family_by_module[module_id] = module_family_id

        return [
            CHWModulePartialCompletionSyncPayload(
                chw_id=chw_id,
                module_id=module_id,
                module_family_id=family_by_module[module_id],
                incomplete_quiz_ids=quiz_ids,
                tenant_id=tenant_by_module.get(module_id),
            )
            for module_id, quiz_ids in sorted(incomplete_by_module.items())
        ]
