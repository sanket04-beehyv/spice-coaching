"""Serialise ingestion_run rows for admin poll and dashboard endpoints."""

from __future__ import annotations

from typing import Any

from mc_contracts.admin_modules import (
    IngestionRunCandidatePayload,
    IngestionRunDetail,
    IngestionRunStepPayload,
    IngestionRunSummary,
    PublishedModuleMergePoll,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.services.run_state_service import (
    FUSION_RUN_TYPE,
    RUN_RUNNING,
    STAGE_CARD_DRAFT,
    STEP_FAILED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    RunStateService,
)


class IngestionRunPresenter:
    """Shared presentation for ``/admin/ingest/*`` poll and ``/admin/ingestion-runs``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._state = RunStateService(session)
        self._candidate_repo = ModuleCandidateRepository(session)

    @staticmethod
    def run_kind(run: IngestionRun) -> str:
        if RunStateService.is_fusion_run(run):
            return FUSION_RUN_TYPE
        return "pipeline"

    @staticmethod
    def step_to_poll_dict(step: IngestionRunStep) -> dict[str, Any]:
        input_summary = step.input_summary_jsonb or {}
        output_summary = step.output_summary_jsonb or {}
        step_dict: dict[str, Any] = {
            "stage": step.stage,
            "status": step.status,
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "input_summary": step.input_summary_jsonb,
            "output_summary": step.output_summary_jsonb,
            "error": step.error_jsonb,
        }
        activity = input_summary.get("activity")
        if activity:
            step_dict["activity"] = activity
        if input_summary.get("fusion") is True:
            step_dict["fusion"] = True
        if step.stage == STAGE_CARD_DRAFT and step.status in (STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED):
            was_merge = output_summary.get("was_published_merge")
            if was_merge is not None:
                step_dict["published_module_merge"] = {
                    "active": False,
                    "was_merge": bool(was_merge),
                    "merged_from_module_id": output_summary.get("merged_from_module_id"),
                }
        return step_dict

    @classmethod
    def step_to_payload(cls, step: IngestionRunStep) -> IngestionRunStepPayload:
        poll = cls.step_to_poll_dict(step)
        merge_raw = poll.get("published_module_merge")
        merge_payload = PublishedModuleMergePoll.model_validate(merge_raw) if merge_raw is not None else None
        return IngestionRunStepPayload(
            id=step.id,
            stage=step.stage,
            status=step.status,
            started_at=step.started_at,
            completed_at=step.completed_at,
            input_summary=step.input_summary_jsonb,
            output_summary=step.output_summary_jsonb,
            error=step.error_jsonb,
            activity=poll.get("activity"),
            fusion=poll.get("fusion"),
            published_module_merge=merge_payload,
        )

    @staticmethod
    def current_activity_from_steps(
        steps: list[IngestionRunStep],
        *,
        run_status: str,
    ) -> dict[str, Any] | None:
        if run_status != RUN_RUNNING:
            return None
        for step in steps:
            if step.status != STEP_RUNNING:
                continue
            input_summary = step.input_summary_jsonb or {}
            activity = input_summary.get("activity")
            if not activity:
                continue
            current: dict[str, Any] = {
                "kind": activity,
                "stage": step.stage,
            }
            if step.stage == STAGE_CARD_DRAFT:
                candidate_id = input_summary.get("candidate_id")
                if candidate_id:
                    current["candidate_id"] = candidate_id
                if input_summary.get("fusion") is True:
                    current["fusion"] = True
                    merged_title = input_summary.get("merged_title")
                    if merged_title:
                        current["merged_title"] = merged_title
            return current
        return None

    @staticmethod
    def present_summary(run: IngestionRun) -> IngestionRunSummary:
        return IngestionRunSummary(
            id=run.id,
            source_document_id=run.source_document_id,
            status=run.status,
            started_at=run.started_at,
            completed_at=run.completed_at,
            error=run.error_jsonb,
        )

    async def _load_run_context(
        self,
        run: IngestionRun,
    ) -> tuple[list[IngestionRunStep], list[IngestionRunCandidatePayload], str]:
        steps = await self._state.list_steps(run.id)
        candidates = [
            IngestionRunCandidatePayload(
                candidate_id=c.id,
                proposed_title=c.proposed_title,
                behavioural_gap_code=c.behavioural_gap_code,
                proposed_module_type=c.proposed_module_type,
                estimated_card_count=c.estimated_card_count,
                estimated_quiz_count=c.estimated_quiz_count,
                quality_flags=c.quality_flags_jsonb,
                ingestion_instruction_rationale=c.ingestion_instruction_rationale,
            )
            for c in await self._candidate_repo.list_candidates_for_run(run.id)
        ]
        return steps, candidates, self.run_kind(run)

    async def present_poll(self, run: IngestionRun) -> dict[str, Any]:
        """JSON payload for ``GET /admin/ingest/{run_id}`` and by-document poll."""
        steps, candidates, run_kind = await self._load_run_context(run)
        payload: dict[str, Any] = {
            "run_id": str(run.id),
            "run_kind": run_kind,
            "source_document_id": str(run.source_document_id),
            "status": run.status,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "error": run.error_jsonb,
            "steps": [self.step_to_poll_dict(s) for s in steps],
            "candidates": [c.model_dump(mode="json") for c in candidates],
        }
        current_activity = self.current_activity_from_steps(steps, run_status=run.status)
        if current_activity is not None:
            payload["current_activity"] = current_activity
        if run_kind == FUSION_RUN_TYPE:
            error = run.error_jsonb or {}
            source_ids = error.get("source_document_ids")
            if source_ids:
                payload["source_document_ids"] = source_ids
        return payload

    async def present_detail(self, run: IngestionRun) -> IngestionRunDetail:
        """Typed dashboard detail for ``GET /admin/ingestion-runs/{run_id}``."""
        steps, candidates, run_kind = await self._load_run_context(run)
        current_activity = self.current_activity_from_steps(steps, run_status=run.status)
        source_document_ids = None
        if run_kind == FUSION_RUN_TYPE:
            source_document_ids = (run.error_jsonb or {}).get("source_document_ids")
        return IngestionRunDetail(
            id=run.id,
            source_document_id=run.source_document_id,
            status=run.status,
            started_at=run.started_at,
            completed_at=run.completed_at,
            error=run.error_jsonb,
            run_kind=run_kind,
            steps=[self.step_to_payload(s) for s in steps],
            candidates=candidates,
            current_activity=current_activity,
            source_document_ids=source_document_ids,
        )
