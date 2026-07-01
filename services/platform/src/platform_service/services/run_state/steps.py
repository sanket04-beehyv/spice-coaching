"""Ingestion run step lifecycle helpers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.services.run_state.constants import (
    _TERMINAL_STEP_STATUSES,
    ALL_STAGES,
    POST_PUBLISH_STAGES,
    RUN_FAILED,
    RUN_PARTIALLY_SUCCEEDED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STEP_FAILED,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    now_utc,
    terminal_run_status_from_steps,
)


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

    async def find_step(
        self,
        run_id: UUID,
        *,
        stage: str,
        input_match: dict[str, Any] | None = None,
    ) -> IngestionRunStep | None:
        result = await self._session.execute(
            select(IngestionRunStep)
            .where(
                IngestionRunStep.ingestion_run_id == run_id,
                IngestionRunStep.stage == stage,
            )
            .order_by(IngestionRunStep.started_at.nullslast())
        )
        steps = list(result.scalars().all())
        if input_match is None:
            return steps[-1] if steps else None
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

    async def fail_step(
        self,
        step_id: UUID,
        *,
        error: dict[str, Any],
    ) -> IngestionRunStep:
        step = await self._session.get(IngestionRunStep, step_id)
        if step is None:
            raise ValueError(f"ingestion_run_step {step_id} not found")
        step.status = STEP_FAILED
        step.completed_at = now_utc()
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
