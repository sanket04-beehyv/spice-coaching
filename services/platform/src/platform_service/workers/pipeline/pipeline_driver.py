"""A→identify→draft pipeline state machine (extracted from PipelineOrchestrator)."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from mc_contracts.errors import ErrorCode

from platform_service.services.run_state_service import (
    RUN_FAILED,
    RUN_PARTIALLY_SUCCEEDED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    ConcurrentRunError,
)
from platform_service.services.source_path_materialize import materialize_local_source_file
from platform_service.workers.pipeline.types import PipelineResult

if TYPE_CHECKING:
    from platform_service.workers.pipeline_orchestrator import PipelineOrchestrator

logger = logging.getLogger(__name__)


async def drive_pipeline(
    orchestrator: PipelineOrchestrator,
    *,
    source_document_id: UUID,
    source_path: str | Path,
    source_type: str,
    primary_language: str,
    triggered_by: UUID | None,
    resume: bool,
    staged_sessions: bool,
    result_box: list[PipelineResult],
    run_id: UUID | None = None,
    identify_chunk_ids: list[str] | None = None,
) -> None:
    """Single A→identify→draft state machine."""
    claim_token = str(uuid.uuid4())
    resolved_run_id: UUID

    async with orchestrator._stage_context(staged_sessions) as orch:
        run = None
        if run_id is not None:
            run = await orch._run_state.activate_queued_run(run_id)
            if run.source_document_id != source_document_id:
                raise ValueError(
                    f"run_id {run_id} belongs to source_document {run.source_document_id}, "
                    f"not {source_document_id}"
                )
            logger.info(
                "Attaching to ingestion_run %s for source_document %s (status=%s)",
                run.id,
                source_document_id,
                run.status,
            )
            await orch._session.commit()
        elif resume:
            run = await orch._run_state.find_resumable_run(source_document_id)
            if run is not None:
                logger.info(
                    "Resuming ingestion_run %s for source_document %s",
                    run.id,
                    source_document_id,
                )
        if run is None:
            run = await orch._run_state.start_run(
                source_document_id=source_document_id,
                triggered_by=triggered_by,
            )
            await orch._session.commit()

        if not await orch._run_state.try_claim_run(run.id, claim_token=claim_token):
            raise ConcurrentRunError(source_document_id, run.id)
        await orch._session.commit()

        resolved_run_id = run.id
        result = PipelineResult(
            run_id=resolved_run_id,
            source_document_id=source_document_id,
            final_status=RUN_SUCCEEDED,
        )
        result_box.append(result)

    local_source_path, staged_cleanup = await materialize_local_source_file(source_path)
    try:
        async with orchestrator._stage_context(staged_sessions) as orch:
            await orch._run_state.refresh_run_claim(resolved_run_id, claim_token=claim_token)
            ok = await orch._run_extract(
                result=result,
                run_id=resolved_run_id,
                source_document_id=source_document_id,
                source_path=local_source_path,
                source_type=source_type,
                primary_language=primary_language,
            )
        if not ok:
            extract_error = next(
                (s.error for s in reversed(result.stages) if s.stage == STAGE_EXTRACT and s.error),
                None,
            )
            run_error: dict[str, object] = {
                "code": ErrorCode.EXTRACT_FAILED.value,
                "failed_stage": STAGE_EXTRACT,
            }
            if extract_error:
                if extract_error.get("reason") is not None:
                    run_error["reason"] = extract_error["reason"]
                if extract_error.get("message") is not None:
                    run_error["message"] = extract_error["message"]
            async with orchestrator._stage_context(staged_sessions) as orch:
                await orch._run_state.complete_run(
                    resolved_run_id,
                    status=RUN_FAILED,
                    error_jsonb=run_error,
                )
                await orch._session.commit()
            result.final_status = RUN_FAILED
            return

        async with orchestrator._stage_context(staged_sessions) as orch:
            await orch._run_state.refresh_run_claim(resolved_run_id, claim_token=claim_token)
            ok, candidates_emitted = await orch._run_identify(
                result=result,
                run_id=resolved_run_id,
                source_document_id=source_document_id,
                identify_chunk_ids=identify_chunk_ids,
            )
        if not ok:
            identify_error = next(
                (s.error for s in reversed(result.stages) if s.stage == STAGE_MODULE_IDENTIFY and s.error),
                None,
            )
            run_error: dict[str, object] = {
                "code": ErrorCode.IDENTIFY_FAILED.value,
                "failed_stage": STAGE_MODULE_IDENTIFY,
            }
            if identify_error:
                if identify_error.get("code") is not None:
                    run_error["code"] = identify_error["code"]
                if identify_error.get("message") is not None:
                    run_error["message"] = identify_error["message"]
            async with orchestrator._stage_context(staged_sessions) as orch:
                await orch._run_state.complete_run(
                    resolved_run_id,
                    status=RUN_PARTIALLY_SUCCEEDED,
                    error_jsonb=run_error,
                )
                await orch._session.commit()
            result.final_status = RUN_PARTIALLY_SUCCEEDED
            return
        result.candidates_emitted = candidates_emitted

        async with orchestrator._stage_context(staged_sessions) as orch:
            await orch._run_state.refresh_run_claim(resolved_run_id, claim_token=claim_token)
            drafts_produced, _draft_failures = await orch._run_drafting(
                result=result,
                run_id=resolved_run_id,
            )
        result.drafts_produced = drafts_produced

        async with orchestrator._stage_context(staged_sessions) as orch:
            await orch._run_state.maybe_finalize_ingestion_run(resolved_run_id)
            await orch._session.commit()
            refreshed = await orch._run_state.get_run(resolved_run_id)
        result.final_status = refreshed.status if refreshed is not None else RUN_RUNNING
    finally:
        try:
            async with orchestrator._stage_context(staged_sessions) as orch:
                await orch._run_state.release_run_claim(resolved_run_id, claim_token=claim_token)
                await orch._session.commit()
        except Exception:
            logger.exception(
                "Failed to release pipeline claim for run_id=%s",
                resolved_run_id,
            )
        if staged_cleanup is not None:
            staged_cleanup.unlink(missing_ok=True)
