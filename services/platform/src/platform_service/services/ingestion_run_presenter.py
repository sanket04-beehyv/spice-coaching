"""Serialise ingestion_run rows for admin poll and dashboard endpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mc_contracts.admin_modules import (
    IngestionRunCandidatePayload,
    IngestionRunDetail,
    IngestionRunStepPayload,
    IngestionRunSummary,
    PublishedModuleMergePoll,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.services.module_presenter import get_card_counts, get_quiz_counts
from platform_service.services.run_state.constants import (
    _PIPELINE_CLAIM_KEY,
    as_error_object,
)
from platform_service.services.run_state_service import (
    FUSION_RUN_TYPE,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STAGE_CARD_DRAFT,
    STEP_AWAITING_INPUT,
    STEP_FAILED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    RunStateService,
)


def _document_label(doc: SourceDocument | None) -> str:
    if doc is None:
        return ""
    filename = (doc.original_filename or "").strip()
    if filename:
        return filename
    return doc.title


def _module_id_from_step(step: IngestionRunStep) -> UUID | None:
    summary = step.output_summary_jsonb or {}
    raw = summary.get("module_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (TypeError, ValueError):
        return None


class IngestionRunPresenter:
    """Shared presentation for ``/admin/ingest/*`` poll and ``/admin/ingestion-runs``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._state = RunStateService(session)
        self._candidate_repo = ModuleCandidateRepository(session)

    @staticmethod
    def _present_run_error(error_jsonb: Any) -> dict[str, Any] | None:
        """Coerce run error metadata for API responses.

        ``error_jsonb`` is typed as an object, but legacy Postgres ``||`` merges
        can leave array values. Internal pipeline-claim keys are stripped.
        """
        error = dict(as_error_object(error_jsonb))
        error.pop(_PIPELINE_CLAIM_KEY, None)
        return error or None

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
            "error_code": step.error_code,
            "error_message": step.error_message,
        }
        activity = input_summary.get("activity")
        if activity:
            step_dict["activity"] = activity
        if input_summary.get("fusion") is True:
            step_dict["fusion"] = True
        if step.stage == STAGE_CARD_DRAFT and step.status == STEP_AWAITING_INPUT:
            step_dict["published_module_merge"] = {
                "active": True,
                "was_merge": False,
                "merged_from_module_id": None,
                "proposed_module_id": output_summary.get("matched_module_id"),
                "proposed_title": output_summary.get("proposed_title"),
                "match_rationale": output_summary.get("match_rationale"),
                "cards_count": output_summary.get("cards_count"),
                "merged_cards_count": output_summary.get("merged_cards_count"),
            }
        elif step.stage == STAGE_CARD_DRAFT and step.status in (
            STEP_SUCCEEDED,
            STEP_FAILED,
            STEP_SKIPPED,
        ):
            was_merge = output_summary.get("was_published_merge")
            if was_merge is not None:
                step_dict["published_module_merge"] = {
                    "active": False,
                    "was_merge": bool(was_merge),
                    "merged_from_module_id": output_summary.get("merged_from_module_id"),
                    "primary_module_id": output_summary.get("module_id"),
                    "secondary_module_id": output_summary.get("secondary_module_id"),
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
            error_code=step.error_code,
            error_message=step.error_message,
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
            if step.status not in (STEP_RUNNING, STEP_AWAITING_INPUT):
                continue
            input_summary = step.input_summary_jsonb or {}
            activity = input_summary.get("activity")
            if step.status == STEP_AWAITING_INPUT and not activity:
                activity = "published_module_merge"
            if not activity:
                continue
            current: dict[str, Any] = {
                "kind": activity,
                "stage": step.stage,
            }
            if step.status == STEP_AWAITING_INPUT:
                current["awaiting_input"] = True
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

    async def present_summaries(self, runs: list[IngestionRun]) -> list[IngestionRunSummary]:
        """Batch-enrich run rows with document label and generated counts."""
        if not runs:
            return []

        doc_ids = list({r.source_document_id for r in runs})
        docs_result = await self._session.execute(
            select(SourceDocument).where(SourceDocument.id.in_(doc_ids))
        )
        docs_by_id = {d.id: d for d in docs_result.scalars().all()}

        succeeded_run_ids = [r.id for r in runs if r.status == RUN_SUCCEEDED]
        module_ids_by_run: dict[UUID, list[UUID]] = {rid: [] for rid in succeeded_run_ids}
        all_module_ids: list[UUID] = []

        if succeeded_run_ids:
            steps_result = await self._session.execute(
                select(IngestionRunStep).where(
                    IngestionRunStep.ingestion_run_id.in_(succeeded_run_ids),
                    IngestionRunStep.stage == STAGE_CARD_DRAFT,
                )
            )
            seen_per_run: dict[UUID, set[UUID]] = {rid: set() for rid in succeeded_run_ids}
            for step in steps_result.scalars().all():
                module_id = _module_id_from_step(step)
                if module_id is None:
                    continue
                seen = seen_per_run[step.ingestion_run_id]
                if module_id in seen:
                    continue
                seen.add(module_id)
                module_ids_by_run[step.ingestion_run_id].append(module_id)
                all_module_ids.append(module_id)

        card_counts = await get_card_counts(self._session, all_module_ids)
        quiz_counts = await get_quiz_counts(self._session, all_module_ids)

        summaries: list[IngestionRunSummary] = []
        for run in runs:
            label = _document_label(docs_by_id.get(run.source_document_id))
            if run.status != RUN_SUCCEEDED:
                summaries.append(
                    IngestionRunSummary(
                        id=run.id,
                        source_document_id=run.source_document_id,
                        status=run.status,
                        started_at=run.started_at,
                        completed_at=run.completed_at,
                        error=self._present_run_error(run.error_jsonb),
                        document_label=label,
                        generated_module_count=0,
                        generated_card_count=0,
                        generated_quiz_count=0,
                    )
                )
                continue

            module_ids = module_ids_by_run.get(run.id, [])
            summaries.append(
                IngestionRunSummary(
                    id=run.id,
                    source_document_id=run.source_document_id,
                    status=run.status,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                    error=self._present_run_error(run.error_jsonb),
                    document_label=label,
                    generated_module_count=len(module_ids),
                    generated_card_count=sum(card_counts.get(mid, 0) for mid in module_ids),
                    generated_quiz_count=sum(quiz_counts.get(mid, 0) for mid in module_ids),
                )
            )
        return summaries

    async def _load_run_context(
        self,
        run: IngestionRun,
    ) -> tuple[list[IngestionRunStep], list[IngestionRunCandidatePayload], str]:
        steps = await self._state.list_steps(run.id)
        candidates = [
            IngestionRunCandidatePayload(
                candidate_id=c.id,
                proposed_title=c.proposed_title,
                domain=c.domain,
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
        """JSON payload for legacy flat poll views (dashboard helpers)."""
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
            error = as_error_object(run.error_jsonb)
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
            source_document_ids = as_error_object(run.error_jsonb).get("source_document_ids")
        summary = (await self.present_summaries([run]))[0]
        return IngestionRunDetail(
            id=summary.id,
            source_document_id=summary.source_document_id,
            status=summary.status,
            started_at=summary.started_at,
            completed_at=summary.completed_at,
            error=summary.error,
            document_label=summary.document_label,
            generated_module_count=summary.generated_module_count,
            generated_card_count=summary.generated_card_count,
            generated_quiz_count=summary.generated_quiz_count,
            run_kind=run_kind,
            steps=[self.step_to_payload(s) for s in steps],
            candidates=candidates,
            current_activity=current_activity,
            source_document_ids=source_document_ids,
        )
