"""A→identify→draft pipeline state machine (extracted from PipelineOrchestrator)."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from platform_service.services.run_state_service import (
    RUN_FAILED,
    RUN_PARTIALLY_SUCCEEDED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STAGE_CARD_DRAFT,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    ConcurrentRunError,
)
from platform_service.services.source_path_materialize import materialize_local_source_file
from platform_service.workers.pipeline.types import PipelineResult, StageOutcome

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
    skip_merge: bool,
    staged_sessions: bool,
    result_box: list[PipelineResult],
) -> None:
    """Single A→identify→draft state machine."""
    run_id: UUID
    claim_token = str(uuid.uuid4())

    async with orchestrator._stage_context(staged_sessions) as orch:
        run = None
        if resume:
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

        run_id = run.id
        result = PipelineResult(
            run_id=run_id,
            source_document_id=source_document_id,
            final_status=RUN_SUCCEEDED,
        )
        result_box.append(result)

    local_source_path, staged_cleanup = await materialize_local_source_file(source_path)
    try:
        async with orchestrator._stage_context(staged_sessions) as orch:
            await orch._run_state.refresh_run_claim(run_id, claim_token=claim_token)
            ok = await orch._run_extract(
                result=result,
                run_id=run_id,
                source_document_id=source_document_id,
                source_path=local_source_path,
                source_type=source_type,
                primary_language=primary_language,
            )
        if not ok:
            async with orchestrator._stage_context(staged_sessions) as orch:
                await orch._run_state.complete_run(
                    run_id,
                    status=RUN_FAILED,
                    error_jsonb={"failed_stage": STAGE_EXTRACT},
                )
                await orch._session.commit()
            result.final_status = RUN_FAILED
            return

        async with orchestrator._stage_context(staged_sessions) as orch:
            await orch._run_state.refresh_run_claim(run_id, claim_token=claim_token)
            ok, candidates_emitted = await orch._run_identify(
                result=result,
                run_id=run_id,
                source_document_id=source_document_id,
            )
        if not ok:
            async with orchestrator._stage_context(staged_sessions) as orch:
                await orch._run_state.complete_run(
                    run_id,
                    status=RUN_PARTIALLY_SUCCEEDED,
                    error_jsonb={"failed_stage": STAGE_MODULE_IDENTIFY},
                )
                await orch._session.commit()
            result.final_status = RUN_PARTIALLY_SUCCEEDED
            return
        result.candidates_emitted = candidates_emitted

        if candidates_emitted == 0:
            async with orchestrator._stage_context(staged_sessions) as orch:
                await orch._run_state.skip_step(
                    run_id=run_id,
                    stage=STAGE_CARD_DRAFT,
                    reason="no_candidates_from_stage_c",
                )
                result.stages.append(
                    StageOutcome(
                        stage=STAGE_CARD_DRAFT,
                        status="skipped",
                        summary={"reason": "no_candidates_from_stage_c"},
                    )
                )
                await orch._run_state.maybe_finalize_ingestion_run(run_id)
                await orch._session.commit()
                refreshed = await orch._run_state.get_run(run_id)
            result.final_status = refreshed.status if refreshed is not None else RUN_SUCCEEDED
            return

        async with orchestrator._stage_context(staged_sessions) as orch:
            await orch._run_state.refresh_run_claim(run_id, claim_token=claim_token)
            drafts_produced, _draft_failures = await orch._run_drafting(
                result=result,
                run_id=run_id,
                skip_merge=skip_merge,
            )
        result.drafts_produced = drafts_produced

        async with orchestrator._stage_context(staged_sessions) as orch:
            await orch._run_state.maybe_finalize_ingestion_run(run_id)
            await orch._session.commit()
            refreshed = await orch._run_state.get_run(run_id)
        result.final_status = refreshed.status if refreshed is not None else RUN_RUNNING
    finally:
        try:
            async with orchestrator._stage_context(staged_sessions) as orch:
                await orch._run_state.release_run_claim(run_id, claim_token=claim_token)
                await orch._session.commit()
        except Exception:
            logger.exception(
                "Failed to release pipeline claim for run_id=%s",
                run_id,
            )
        if staged_cleanup is not None:
            staged_cleanup.unlink(missing_ok=True)
