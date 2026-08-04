"""Admin ingest failed-stage retry — clear/replace artifacts and re-enqueue."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.ingest_batch_poll_presenter import _retry_targets_for_run
from platform_service.services.ingest_enqueue_service import (
    enqueue_fusion_retry,
    enqueue_pipeline_resume,
    enqueue_post_publish_step_retry,
    enqueue_thumbnail_retry,
)
from platform_service.services.run_state.constants import as_error_object
from platform_service.services.run_state_service import (
    ALL_STAGES,
    POST_PUBLISH_STAGES,
    RUN_RUNNING,
    STAGE_CARD_DRAFT,
    STAGE_CROSS_SOURCE_FUSION,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    STAGE_THUMBNAIL,
    STEP_FAILED,
    RunStateService,
)

RetryOutcomeKind = Literal["retry_queued", "noop"]

_CANDIDATE_SCOPED_STAGES = frozenset({STAGE_CARD_DRAFT, *POST_PUBLISH_STAGES})


@dataclass(frozen=True)
class IngestRetryResult:
    status: RetryOutcomeKind
    batch_id: UUID
    run_id: UUID
    stage: str
    candidate_id: UUID | None = None
    chunk_id: str | None = None
    reason: str | None = None
    poll_url: str | None = None


@dataclass(frozen=True)
class IngestRetryStageResult:
    run_id: UUID
    stage: str
    status: str
    candidate_id: UUID | None = None
    chunk_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class IngestRetryBatchResult:
    batch_id: UUID
    results: list[IngestRetryStageResult]
    poll_url: str


class IngestRetryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._run_state = RunStateService(session)
        self._sources = SourceRepository(session)
        self._candidates = ModuleCandidateRepository(session)

    async def retry_batch(self, batch_id: UUID) -> IngestRetryBatchResult:
        batch = await self._run_state.get_batch(batch_id)
        if batch is None:
            raise AppError(ErrorCode.BATCH_NOT_FOUND.value, "ingest batch not found", status=404)

        targets = await self._collect_retry_targets(batch_id)
        results: list[IngestRetryStageResult] = []
        for target in targets:
            results.append(await self._retry_one_target(batch_id=batch_id, target=target))

        return IngestRetryBatchResult(
            batch_id=batch_id,
            results=results,
            poll_url=get_settings().api_path(f"/admin/ingest/batches/{batch_id}"),
        )

    async def _collect_retry_targets(self, batch_id: UUID) -> list[dict[str, Any]]:
        runs = await self._run_state.list_runs_for_batch(batch_id)
        targets: list[dict[str, Any]] = []
        for run in runs:
            steps = await self._run_state.list_steps(run.id)
            blocked = run.status == RUN_RUNNING and self._run_state.has_active_pipeline_claim(run)
            targets.extend(
                _retry_targets_for_run(
                    batch_id=batch_id,
                    run=run,
                    steps=steps,
                    blocked_by_active_claim=blocked,
                )
            )
        return targets

    async def _retry_one_target(
        self,
        *,
        batch_id: UUID,
        target: dict[str, Any],
    ) -> IngestRetryStageResult:
        run_id = UUID(str(target["run_id"]))
        stage = str(target["stage"])
        candidate_raw = target.get("candidate_id")
        candidate_id = UUID(str(candidate_raw)) if candidate_raw else None
        chunk_id = str(target["chunk_id"]) if target.get("chunk_id") else None

        try:
            outcome = await self.retry(
                batch_id=batch_id,
                run_id=run_id,
                stage=stage,
                candidate_id=candidate_id,
                chunk_id=chunk_id,
            )
        except AppError as exc:
            # Upstream stage retry may have cascaded away this step; treat as noop.
            if exc.code in {
                ErrorCode.STEP_NOT_FOUND.value,
                ErrorCode.RUN_NOT_FOUND.value,
            }:
                return IngestRetryStageResult(
                    run_id=run_id,
                    stage=stage,
                    status="noop",
                    candidate_id=candidate_id,
                    chunk_id=chunk_id,
                    reason=exc.code,
                )
            raise

        return IngestRetryStageResult(
            run_id=outcome.run_id,
            stage=outcome.stage,
            status=outcome.status,
            candidate_id=outcome.candidate_id,
            chunk_id=outcome.chunk_id,
            reason=outcome.reason,
        )

    async def retry(
        self,
        *,
        batch_id: UUID,
        run_id: UUID,
        stage: str,
        candidate_id: UUID | None = None,
        chunk_id: str | None = None,
    ) -> IngestRetryResult:
        batch = await self._run_state.get_batch(batch_id)
        if batch is None:
            raise AppError(ErrorCode.BATCH_NOT_FOUND.value, "ingest batch not found", status=404)

        run = await self._run_state.get_run(run_id)
        if run is None or run.ingest_batch_id != batch_id:
            raise AppError(ErrorCode.RUN_NOT_FOUND.value, "ingestion run not found in batch", status=404)

        if stage not in ALL_STAGES:
            raise AppError(ErrorCode.UNKNOWN_STAGE.value, f"unknown stage: {stage!r}", status=422)

        if stage in _CANDIDATE_SCOPED_STAGES and candidate_id is None:
            raise AppError(
                ErrorCode.CANDIDATE_REQUIRED.value,
                f"candidate_id is required when retrying stage {stage!r}",
                status=422,
            )

        if chunk_id is not None and stage != STAGE_MODULE_IDENTIFY:
            raise AppError(
                ErrorCode.CHUNK_ID_INVALID.value,
                "chunk_id is only valid when retrying module_identify",
                status=422,
            )

        if stage == STAGE_MODULE_IDENTIFY and chunk_id is not None:
            return await self._retry_identify_chunk(
                batch_id=batch_id,
                run=run,
                chunk_id=chunk_id,
            )

        input_match: dict[str, Any] | None = None
        if candidate_id is not None:
            input_match = {"candidate_id": str(candidate_id)}

        if stage == STAGE_MODULE_IDENTIFY:
            step = await self._run_state.find_module_identify_parent(run_id)
        else:
            step = await self._run_state.find_step(run_id, stage=stage, input_match=input_match)
        if step is None:
            raise AppError(ErrorCode.STEP_NOT_FOUND.value, f"no {stage!r} step found for run", status=404)

        if step.status != STEP_FAILED:
            return IngestRetryResult(
                status="noop",
                batch_id=batch_id,
                run_id=run_id,
                stage=stage,
                candidate_id=candidate_id,
                reason="step_not_failed",
            )

        if run.status == RUN_RUNNING and self._run_state.has_active_pipeline_claim(run):
            return IngestRetryResult(
                status="noop",
                batch_id=batch_id,
                run_id=run_id,
                stage=stage,
                candidate_id=candidate_id,
                reason="already_running",
            )

        await self._clear_artifacts(
            run_id=run_id,
            source_document_id=run.source_document_id,
            stage=stage,
            candidate_id=candidate_id,
        )

        if stage in POST_PUBLISH_STAGES:
            await self._run_state.reset_step_for_retry(step.id)
            module_id = self._module_id_from_step(step)
            if module_id is None:
                raise AppError(
                    ErrorCode.MODULE_ID_MISSING.value,
                    "post-publish step has no module_id to re-enqueue",
                    status=422,
                )
            step_id = step.id
            await self._run_state.reopen_run_for_retry(run_id)
            await self._run_state.refresh_batch_status(batch_id)
            await self._session.commit()
            enqueue_post_publish_step_retry(
                stage=stage,
                module_id=module_id,
                step_id=step_id,
                candidate_id=candidate_id,
            )
        else:
            enqueue_args = await self._prepare_enqueue_args(
                batch_id=batch_id,
                run=run,
                stage=stage,
            )
            await self._run_state.delete_steps_for_stage_retry(
                run_id,
                stage=stage,
                candidate_id=candidate_id if stage in _CANDIDATE_SCOPED_STAGES else None,
            )
            await self._run_state.reopen_run_for_retry(run_id)
            await self._run_state.refresh_batch_status(batch_id)
            await self._session.commit()
            self._dispatch_enqueue(stage=stage, args=enqueue_args)

        return IngestRetryResult(
            status="retry_queued",
            batch_id=batch_id,
            run_id=run_id,
            stage=stage,
            candidate_id=candidate_id,
            poll_url=get_settings().api_path(f"/admin/ingest/batches/{batch_id}"),
        )

    async def _retry_identify_chunk(
        self,
        *,
        batch_id: UUID,
        run: Any,
        chunk_id: str,
    ) -> IngestRetryResult:
        run_id = run.id
        step = await self._run_state.find_step(
            run_id,
            stage=STAGE_MODULE_IDENTIFY,
            input_match={"chunk_id": chunk_id},
        )
        if step is None:
            raise AppError(
                ErrorCode.STEP_NOT_FOUND.value,
                f"no module_identify chunk step for chunk_id={chunk_id!r}",
                status=404,
            )

        if step.status != STEP_FAILED:
            return IngestRetryResult(
                status="noop",
                batch_id=batch_id,
                run_id=run_id,
                stage=STAGE_MODULE_IDENTIFY,
                chunk_id=chunk_id,
                reason="step_not_failed",
            )

        if run.status == RUN_RUNNING and self._run_state.has_active_pipeline_claim(run):
            return IngestRetryResult(
                status="noop",
                batch_id=batch_id,
                run_id=run_id,
                stage=STAGE_MODULE_IDENTIFY,
                chunk_id=chunk_id,
                reason="already_running",
            )

        progressed = await self._progressed_candidate_ids(run_id)
        deleted_ids = await self._candidates.delete_candidates_for_chunk(
            run_id,
            chunk_id=chunk_id,
            exclude_ids=progressed,
        )
        for cand_id in deleted_ids:
            mids = await self._run_state.module_ids_for_candidate_steps(run_id, candidate_id=cand_id)
            await self._delete_modules(mids)
            await self._run_state.delete_steps_for_stage_retry(
                run_id,
                stage=STAGE_CARD_DRAFT,
                candidate_id=cand_id,
            )

        await self._run_state.reset_step_for_retry(step.id)
        enqueue_args = await self._prepare_enqueue_args(
            batch_id=batch_id,
            run=run,
            stage=STAGE_MODULE_IDENTIFY,
        )
        enqueue_args["identify_chunk_ids"] = [chunk_id]
        await self._run_state.reopen_run_for_retry(run_id)
        await self._run_state.refresh_batch_status(batch_id)
        await self._session.commit()
        enqueue_pipeline_resume(**enqueue_args)

        return IngestRetryResult(
            status="retry_queued",
            batch_id=batch_id,
            run_id=run_id,
            stage=STAGE_MODULE_IDENTIFY,
            chunk_id=chunk_id,
            poll_url=get_settings().api_path(f"/admin/ingest/batches/{batch_id}"),
        )

    async def _progressed_candidate_ids(self, run_id: UUID) -> set[UUID]:
        """Candidates that already have a card_draft step (any status)."""
        steps = await self._run_state.list_steps(run_id)
        found: set[UUID] = set()
        for step in steps:
            if step.stage != STAGE_CARD_DRAFT:
                continue
            raw = (step.input_summary_jsonb or {}).get("candidate_id")
            if raw:
                found.add(UUID(str(raw)))
        return found

    async def _clear_artifacts(
        self,
        *,
        run_id: UUID,
        source_document_id: UUID,
        stage: str,
        candidate_id: UUID | None,
    ) -> None:
        if stage == STAGE_THUMBNAIL:
            await self._sources.clear_thumbnail_storage_path(source_document_id)
            return

        if stage == STAGE_EXTRACT:
            module_ids = await self._run_state.module_ids_for_candidate_steps(run_id)
            await self._delete_modules(module_ids)
            await self._candidates.delete_candidates_for_run(run_id)
            await self._sources.clear_extract_artifacts(source_document_id)
            return

        if stage == STAGE_MODULE_IDENTIFY:
            module_ids = await self._run_state.module_ids_for_candidate_steps(run_id)
            await self._delete_modules(module_ids)
            await self._candidates.delete_candidates_for_run(run_id)
            return

        if stage == STAGE_CARD_DRAFT:
            assert candidate_id is not None
            module_ids = await self._run_state.module_ids_for_candidate_steps(
                run_id, candidate_id=candidate_id
            )
            await self._delete_modules(module_ids)
            return

        if stage == STAGE_CROSS_SOURCE_FUSION:
            module_ids = await self._run_state.module_ids_for_candidate_steps(run_id)
            await self._delete_modules(module_ids)
            await self._candidates.delete_candidates_for_run(run_id)
            return

        # Post-publish: module stays; only the step is reset by the caller.

    async def _prepare_enqueue_args(
        self,
        *,
        batch_id: UUID,
        run: Any,
        stage: str,
    ) -> dict[str, Any]:
        """Snapshot enqueue inputs before commit (expire_on_commit would drop them)."""
        if stage == STAGE_THUMBNAIL:
            doc = await self._session.get(SourceDocument, run.source_document_id)
            if doc is None:
                raise AppError(ErrorCode.SOURCE_NOT_FOUND.value, "source_document not found", status=422)
            return {
                "source_document_id": run.source_document_id,
                "source_path": doc.original_storage_path,
                "source_type": doc.source_type,
                "run_id": run.id,
            }

        if stage == STAGE_CROSS_SOURCE_FUSION:
            source_ids = as_error_object(run.error_jsonb).get("source_document_ids") or []
            if len(source_ids) < 2:
                raise AppError(
                    ErrorCode.FUSION_SOURCES_MISSING.value,
                    "fusion run is missing source_document_ids",
                    status=422,
                )
            return {
                "source_document_ids": [UUID(str(d)) for d in source_ids],
                "batch_id": batch_id,
                "fusion_run_id": run.id,
            }

        doc = await self._session.get(SourceDocument, run.source_document_id)
        if doc is None:
            raise AppError(ErrorCode.SOURCE_NOT_FOUND.value, "source_document not found", status=422)
        return {
            "source_document_id": run.source_document_id,
            "source_path": doc.original_storage_path,
            "source_type": doc.source_type,
            "primary_language": doc.primary_language,
            "run_id": run.id,
            "batch_id": batch_id,
        }

    @staticmethod
    def _dispatch_enqueue(*, stage: str, args: dict[str, Any]) -> None:
        if stage == STAGE_THUMBNAIL:
            enqueue_thumbnail_retry(**args)
            return
        if stage == STAGE_CROSS_SOURCE_FUSION:
            enqueue_fusion_retry(**args)
            return
        enqueue_pipeline_resume(**args)

    async def _delete_modules(self, module_ids: list[UUID]) -> None:
        if not module_ids:
            return
        await self._session.execute(delete(Module).where(Module.id.in_(module_ids)))
        await self._session.flush()

    @staticmethod
    def _module_id_from_step(step: Any) -> UUID | None:
        payload = step.input_summary_jsonb or {}
        raw = payload.get("module_id")
        if raw is None and step.output_summary_jsonb:
            raw = (step.output_summary_jsonb or {}).get("module_id")
        if not raw:
            return None
        return UUID(str(raw))
