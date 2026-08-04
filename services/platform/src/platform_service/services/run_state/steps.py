"""Ingestion run step lifecycle helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.services.run_state.constants import (
    _DEFAULT_CLAIM_STALE_SECONDS,
    _PIPELINE_CLAIM_KEY,
    _TERMINAL_STEP_STATUSES,
    ALL_STAGES,
    POST_PUBLISH_STAGES,
    RUN_FAILED,
    RUN_PARTIALLY_SUCCEEDED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STAGE_CARD_DRAFT,
    STAGE_CROSS_SOURCE_FUSION,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    STAGE_THUMBNAIL,
    STEP_AWAITING_INPUT,
    STEP_FAILED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    as_error_object,
    now_utc,
    terminal_run_status_from_steps,
)

# Stages removed when retrying a given stage (the stage itself is also removed).
_DOWNSTREAM_STAGES: dict[str, frozenset[str]] = {
    STAGE_THUMBNAIL: frozenset(),
    STAGE_EXTRACT: frozenset({STAGE_MODULE_IDENTIFY, STAGE_CARD_DRAFT, *POST_PUBLISH_STAGES}),
    STAGE_MODULE_IDENTIFY: frozenset({STAGE_CARD_DRAFT, *POST_PUBLISH_STAGES}),
    STAGE_CARD_DRAFT: frozenset(POST_PUBLISH_STAGES),
    STAGE_CROSS_SOURCE_FUSION: frozenset({STAGE_CARD_DRAFT, *POST_PUBLISH_STAGES}),
    **{stage: frozenset() for stage in POST_PUBLISH_STAGES},
}

_TERMINAL_ERROR_KEYS = frozenset(
    {"failed_stage", "failed_stages", "draft_failures", "drafts_produced", "detail", "code"}
)


def is_module_identify_chunk_step(step: IngestionRunStep) -> bool:
    """True when the step is a per-chunk module_identify child (has chunk_id)."""
    payload = step.input_summary_jsonb or {}
    return "chunk_id" in payload


class RunStepMixin:
    _session: AsyncSession

    async def get_run(self, run_id: UUID) -> IngestionRun | None:
        return await self._session.get(IngestionRun, run_id)

    async def complete_run(
        self,
        run_id: UUID,
        *,
        status: str,
        error_jsonb: dict[str, Any] | None = None,
    ) -> IngestionRun:
        if status not in (RUN_SUCCEEDED, RUN_FAILED, RUN_PARTIALLY_SUCCEEDED):
            raise ValueError(f"invalid terminal run status: {status!r}")
        run = await self.get_run(run_id)
        if run is None:
            raise ValueError(f"ingestion_run {run_id} not found")
        run.status = status
        run.completed_at = now_utc()
        if error_jsonb is not None:
            run.error_jsonb = error_jsonb
        await self._session.flush()
        return run

    async def list_steps(self, run_id: UUID) -> list[IngestionRunStep]:
        result = await self._session.execute(
            select(IngestionRunStep)
            .where(IngestionRunStep.ingestion_run_id == run_id)
            .order_by(IngestionRunStep.started_at.nullslast(), IngestionRunStep.id)
        )
        return list(result.scalars().all())

    async def find_module_identify_parent(self, run_id: UUID) -> IngestionRunStep | None:
        """Return the latest parent module_identify step (no chunk_id in input)."""
        parents = [
            s
            for s in await self.list_steps(run_id)
            if s.stage == STAGE_MODULE_IDENTIFY and not is_module_identify_chunk_step(s)
        ]
        return parents[-1] if parents else None

    async def list_module_identify_chunk_steps(self, run_id: UUID) -> list[IngestionRunStep]:
        """Return all per-chunk module_identify steps for a run (stable order)."""
        return [
            s
            for s in await self.list_steps(run_id)
            if s.stage == STAGE_MODULE_IDENTIFY and is_module_identify_chunk_step(s)
        ]

    async def is_module_identify_fully_succeeded(self, run_id: UUID) -> bool:
        """True when parent succeeded and every chunk child succeeded (or no children)."""
        parent = await self.find_module_identify_parent(run_id)
        if parent is None or parent.status != STEP_SUCCEEDED:
            return False
        chunks = await self.list_module_identify_chunk_steps(run_id)
        if not chunks:
            return True
        return all(s.status == STEP_SUCCEEDED for s in chunks)

    async def find_step(
        self,
        run_id: UUID,
        *,
        stage: str,
        input_match: dict[str, Any] | None = None,
    ) -> IngestionRunStep | None:
        """Return the latest matching step (by started_at, then id)."""
        result = await self._session.execute(
            select(IngestionRunStep)
            .where(
                IngestionRunStep.ingestion_run_id == run_id,
                IngestionRunStep.stage == stage,
            )
            .order_by(IngestionRunStep.started_at.desc().nullslast(), IngestionRunStep.id.desc())
        )
        steps = list(result.scalars().all())
        if input_match is None:
            return steps[0] if steps else None
        for step in steps:
            payload = step.input_summary_jsonb or {}
            if all(payload.get(k) == v for k, v in input_match.items()):
                return step
        return None

    async def start_step(
        self,
        *,
        run_id: UUID,
        stage: str,
        input_summary: dict[str, Any] | None = None,
    ) -> IngestionRunStep:
        if stage not in ALL_STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        step = IngestionRunStep(
            ingestion_run_id=run_id,
            stage=stage,
            status=STEP_RUNNING,
            started_at=now_utc(),
            input_summary_jsonb=input_summary,
        )
        self._session.add(step)
        await self._session.flush()
        return step

    async def patch_step_input_summary(
        self,
        step_id: UUID,
        patch: dict[str, Any],
    ) -> IngestionRunStep:
        step = await self._session.get(IngestionRunStep, step_id)
        if step is None:
            raise ValueError(f"ingestion_run_step {step_id} not found")
        base = dict(step.input_summary_jsonb or {})
        base.update(patch)
        step.input_summary_jsonb = base
        await self._session.flush()
        return step

    async def complete_step(
        self,
        step_id: UUID,
        *,
        output_summary: dict[str, Any] | None = None,
        llm_call_id: UUID | None = None,
    ) -> IngestionRunStep:
        step = await self._session.get(IngestionRunStep, step_id)
        if step is None:
            raise ValueError(f"ingestion_run_step {step_id} not found")
        step.status = STEP_SUCCEEDED
        step.completed_at = now_utc()
        if output_summary is not None:
            step.output_summary_jsonb = output_summary
        if llm_call_id is not None:
            step.llm_call_id = llm_call_id
        await self._session.flush()
        return step

    async def park_step_awaiting_input(
        self,
        step_id: UUID,
        *,
        output_summary: dict[str, Any],
    ) -> IngestionRunStep:
        """Park a running step until an admin decision resumes it."""
        step = await self._session.get(IngestionRunStep, step_id)
        if step is None:
            raise ValueError(f"ingestion_run_step {step_id} not found")
        step.status = STEP_AWAITING_INPUT
        step.completed_at = None
        step.output_summary_jsonb = output_summary
        step.error_jsonb = None
        step.error_code = None
        step.error_message = None
        await self._session.flush()
        return step

    async def fail_step(
        self,
        step_id: UUID,
        *,
        error_code: str,
        error_message: str,
        error: dict[str, Any] | None = None,
    ) -> IngestionRunStep:
        step = await self._session.get(IngestionRunStep, step_id)
        if step is None:
            raise ValueError(f"ingestion_run_step {step_id} not found")
        step.status = STEP_FAILED
        step.completed_at = now_utc()
        step.error_code = error_code
        step.error_message = error_message
        step.error_jsonb = error
        await self._session.flush()
        return step

    async def skip_step(
        self,
        *,
        run_id: UUID,
        stage: str,
        reason: str,
        input_summary: dict[str, Any] | None = None,
    ) -> IngestionRunStep:
        if stage not in ALL_STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        step = IngestionRunStep(
            ingestion_run_id=run_id,
            stage=stage,
            status=STEP_SKIPPED,
            started_at=now_utc(),
            completed_at=now_utc(),
            input_summary_jsonb=input_summary,
            output_summary_jsonb={"skipped_reason": reason},
        )
        self._session.add(step)
        await self._session.flush()
        return step

    async def is_stage_succeeded(
        self,
        run_id: UUID,
        *,
        stage: str,
        input_match: dict[str, Any] | None = None,
    ) -> bool:
        step = await self.find_step(run_id, stage=stage, input_match=input_match)
        return step is not None and step.status == STEP_SUCCEEDED

    async def maybe_finalize_ingestion_run(self, run_id: UUID) -> bool:
        run = await self.get_run(run_id)
        if run is None or run.status != RUN_RUNNING:
            return False

        all_steps = await self.list_steps(run_id)
        # Thumbnail is best-effort; all other non-terminal steps (incl.
        # awaiting_input) block run finalization.
        blocking = [
            s for s in all_steps if s.stage != STAGE_THUMBNAIL and s.status not in _TERMINAL_STEP_STATUSES
        ]
        if blocking:
            return False

        post_publish = [s for s in all_steps if s.stage in POST_PUBLISH_STAGES]
        if post_publish:
            if any(s.status not in _TERMINAL_STEP_STATUSES for s in post_publish):
                return False

        final_status, error_jsonb = terminal_run_status_from_steps(all_steps)
        run = await self.get_run(run_id)
        if run is None or run.status != RUN_RUNNING:
            return False
        await self.complete_run(run_id, status=final_status, error_jsonb=error_jsonb)
        return True

    async def run_has_awaiting_input(self, run_id: UUID) -> bool:
        steps = await self.list_steps(run_id)
        return any(s.status == STEP_AWAITING_INPUT for s in steps)

    def has_active_pipeline_claim(
        self,
        run: IngestionRun,
        *,
        stale_after_seconds: int = _DEFAULT_CLAIM_STALE_SECONDS,
    ) -> bool:
        """True when the run holds a non-stale pipeline claim."""
        claim = as_error_object(run.error_jsonb).get(_PIPELINE_CLAIM_KEY)
        if not isinstance(claim, dict):
            return False
        claimed_at_raw = claim.get("claimed_at")
        if not claimed_at_raw:
            return False
        try:
            claimed_at = datetime.fromisoformat(str(claimed_at_raw))
        except ValueError:
            return False
        return claimed_at >= now_utc() - timedelta(seconds=stale_after_seconds)

    async def reopen_run_for_retry(self, run_id: UUID) -> IngestionRun:
        """Re-open a terminal/partial run so a worker can claim and resume it."""
        run = await self.get_run(run_id)
        if run is None:
            raise ValueError(f"ingestion_run {run_id} not found")
        run.status = RUN_RUNNING
        run.completed_at = None
        error = dict(as_error_object(run.error_jsonb))
        error.pop(_PIPELINE_CLAIM_KEY, None)
        for key in _TERMINAL_ERROR_KEYS:
            error.pop(key, None)
        run.error_jsonb = error or None
        await self._session.flush()
        return run

    async def reset_step_for_retry(self, step_id: UUID) -> IngestionRunStep:
        """Re-open a failed step in place (used for post-publish Celery re-enqueue)."""
        step = await self._session.get(IngestionRunStep, step_id)
        if step is None:
            raise ValueError(f"ingestion_run_step {step_id} not found")
        step.status = STEP_RUNNING
        step.started_at = now_utc()
        step.completed_at = None
        step.error_jsonb = None
        step.error_code = None
        step.error_message = None
        step.output_summary_jsonb = None
        await self._session.flush()
        return step

    async def delete_steps_for_stage_retry(
        self,
        run_id: UUID,
        *,
        stage: str,
        candidate_id: UUID | None = None,
    ) -> int:
        """Delete the target stage step(s) and dependent downstream steps.

        When ``candidate_id`` is set, only steps whose input_summary matches
        that candidate are removed (card_draft / post-publish branches).
        """
        if stage not in ALL_STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        stages_to_delete = {stage, *_DOWNSTREAM_STAGES.get(stage, frozenset())}
        all_steps = await self.list_steps(run_id)
        ids: list[UUID] = []
        candidate_str = str(candidate_id) if candidate_id is not None else None
        for step in all_steps:
            if step.stage not in stages_to_delete:
                continue
            if candidate_str is not None:
                payload = step.input_summary_jsonb or {}
                if payload.get("candidate_id") != candidate_str:
                    continue
            ids.append(step.id)
        if not ids:
            return 0
        await self._session.execute(delete(IngestionRunStep).where(IngestionRunStep.id.in_(ids)))
        await self._session.flush()
        return len(ids)

    async def module_ids_for_candidate_steps(
        self,
        run_id: UUID,
        *,
        candidate_id: UUID | None = None,
    ) -> list[UUID]:
        """Collect module_ids recorded on card_draft / post-publish step summaries."""
        all_steps = await self.list_steps(run_id)
        candidate_str = str(candidate_id) if candidate_id is not None else None
        found: list[UUID] = []
        seen: set[UUID] = set()
        for step in all_steps:
            if step.stage not in (STAGE_CARD_DRAFT, *POST_PUBLISH_STAGES):
                continue
            payload = step.input_summary_jsonb or {}
            if candidate_str is not None and payload.get("candidate_id") != candidate_str:
                continue
            raw = payload.get("module_id")
            if raw is None and step.output_summary_jsonb:
                raw = (step.output_summary_jsonb or {}).get("module_id")
            if not raw:
                continue
            mid = UUID(str(raw))
            if mid not in seen:
                seen.add(mid)
                found.append(mid)
        return found


__all__ = [
    "RunStepMixin",
    "is_module_identify_chunk_step",
]
