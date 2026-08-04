"""Per-stage runners — call stage services and return outcomes.

Resume/skip policy and SSE event emission stay in ``PipelineOrchestrator``;
these runners own step lifecycle (start → execute → complete/fail) for one
stage invocation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from mc_contracts.errors import ErrorCode
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.services.ingestion_cardinality import load_batch_for_run
from platform_service.services.run_state_service import (
    STAGE_CARD_DRAFT,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    STEP_FAILED,
    STEP_SUCCEEDED,
    RunStateService,
)
from platform_service.workers.pipeline.types import StageOutcome
from platform_service.workers.stage_a_extract import Stage1ExtractionError, StageAExtractor
from platform_service.workers.stage_c_identify import StageCOrchestrator
from platform_service.workers.stage_d_draft import StageDOrchestrator

logger = logging.getLogger(__name__)


class ExtractStageRunner:
    """Stage A — extract pages and assemble outline."""

    def __init__(self, session: AsyncSession, run_state: RunStateService) -> None:
        self._session = session
        self._run_state = run_state

    async def run(
        self,
        *,
        stage_a: StageAExtractor,
        run_id: UUID,
        source_document_id: UUID,
        source_path: str | Path,
        source_type: str,
        primary_language: str,
    ) -> tuple[bool, StageOutcome]:
        step = await self._run_state.start_step(
            run_id=run_id,
            stage=STAGE_EXTRACT,
            input_summary={
                "source_document_id": str(source_document_id),
                "source_type": source_type,
            },
        )
        # Capture PK before commit — expire_on_commit=True (default) would
        # otherwise force a lazy-load on step.id from inside the failure path,
        # which raises MissingGreenlet on async sessions.
        step_id = step.id
        await self._session.commit()  # mark stage as 'running' for observers
        try:
            stage_a_result = await stage_a.run(
                source_document_id=source_document_id,
                source_path=source_path,
                source_type=source_type,
                primary_language=primary_language,
            )
        except Stage1ExtractionError as exc:
            # Typed: document_empty / vision_recovery_failed / etc. Caller can
            # branch on error_jsonb.reason instead of parsing the message string.
            logger.error(
                "Stage 1 contract violation for source_document %s: %s",
                source_document_id,
                exc,
            )
            await self._session.rollback()
            error = {
                "type": type(exc).__name__,
                "reason": getattr(exc, "reason", "extract_failed"),
                "message": str(exc)[:500],
            }
            await self._run_state.fail_step(
                step_id,
                error_code=ErrorCode.EXTRACT_FAILED.value,
                error_message=str(exc)[:500],
                error=error,
            )
            await self._session.commit()
            return False, StageOutcome(stage=STAGE_EXTRACT, status="failed", error=error)
        except Exception as exc:
            logger.exception("Stage 1 crashed for source_document %s", source_document_id)
            await self._session.rollback()
            error = {"type": type(exc).__name__, "message": str(exc)[:500]}
            await self._run_state.fail_step(
                step_id,
                error_code=ErrorCode.EXTRACT_FAILED.value,
                error_message=str(exc)[:500],
                error=error,
            )
            await self._session.commit()
            return False, StageOutcome(stage=STAGE_EXTRACT, status="failed", error=error)
        summary = {
            "total_pages": stage_a_result.total_pages,
            "pages_persisted": stage_a_result.pages_persisted,
            "extraction_method_counts": stage_a_result.extraction_method_counts,
            # Outline assembly is now part of Stage 1; surface the section
            # count in the step summary so empty-document failures and
            # outline metrics are observable in run history.
            "outline_section_count": stage_a_result.outline_section_count,
        }
        await self._run_state.complete_step(step_id, output_summary=summary)
        await self._session.commit()  # persist Stage 1 output before Stage 2 starts
        return True, StageOutcome(stage=STAGE_EXTRACT, status="succeeded", summary=summary)


class IdentifyStageRunner:
    """Stage C — identify module candidates from extracted content."""

    def __init__(self, session: AsyncSession, run_state: RunStateService) -> None:
        self._session = session
        self._run_state = run_state

    async def run(
        self,
        *,
        stage_c: StageCOrchestrator,
        run_id: UUID,
        source_document_id: UUID,
        identify_chunk_ids: list[str] | None = None,
    ) -> tuple[bool, int, StageOutcome]:
        batch = await load_batch_for_run(self._session, run_id)
        ingestion_instructions_present = bool(batch and batch.ingestion_instructions)
        cardinality_present = bool(
            batch and (batch.cards_per_module is not None or batch.quizzes_per_module is not None)
        )
        selective = bool(identify_chunk_ids)
        if selective:
            parent = await self._run_state.find_module_identify_parent(run_id)
            if parent is None:
                error = {
                    "type": "IdentifyParentMissing",
                    "message": "module_identify parent step missing for chunk retry",
                }
                return (
                    False,
                    0,
                    StageOutcome(stage=STAGE_MODULE_IDENTIFY, status="failed", error=error),
                )
            step_id = parent.id
            # Keep parent visible as running while the chunk retry executes.
            await self._run_state.reset_step_for_retry(step_id)
        else:
            step = await self._run_state.start_step(
                run_id=run_id,
                stage=STAGE_MODULE_IDENTIFY,
                input_summary={
                    "source_document_ids": [str(source_document_id)],
                    "ingestion_instructions_present": ingestion_instructions_present,
                    "cardinality_targets_present": cardinality_present,
                },
            )
            step_id = step.id
        await self._session.commit()  # mark stage as 'running' for observers
        chunk_id_set = set(identify_chunk_ids) if identify_chunk_ids else None
        try:
            stage_c_result = await stage_c.run(
                ingestion_run_id=run_id,
                source_document_ids=[source_document_id],
                chunk_ids=chunk_id_set,
            )
        except Exception as exc:
            logger.exception("Stage C failed for run %s", run_id)
            await self._session.rollback()
            error = {"type": type(exc).__name__, "message": str(exc)[:500]}
            await self._run_state.fail_step(
                step_id,
                error_code=ErrorCode.IDENTIFY_FAILED.value,
                error_message=str(exc)[:500],
                error=error,
            )
            await self._session.commit()
            return (
                False,
                0,
                StageOutcome(stage=STAGE_MODULE_IDENTIFY, status="failed", error=error),
            )

        # Aggregate chunk telemetry from all chunk steps (full + selective).
        all_chunk_steps = await self._run_state.list_module_identify_chunk_steps(run_id)
        chunks_attempted = len(all_chunk_steps) if all_chunk_steps else stage_c_result.chunks_attempted
        chunks_succeeded = sum(1 for s in all_chunk_steps if s.status == STEP_SUCCEEDED)
        chunks_failed = sum(1 for s in all_chunk_steps if s.status == STEP_FAILED)
        if not all_chunk_steps:
            chunks_succeeded = stage_c_result.chunks_succeeded
            chunks_failed = stage_c_result.chunks_failed

        total_candidates = len(await ModuleCandidateRepository(self._session).list_candidates_for_run(run_id))
        emitted_candidates = total_candidates if selective else stage_c_result.candidates_emitted
        summary = {
            "candidates_emitted": emitted_candidates,
            "candidates_flagged": stage_c_result.candidates_flagged,
            "flag_counts": stage_c_result.flag_counts,
            "chunks_attempted": chunks_attempted,
            "chunks_succeeded": chunks_succeeded,
            "chunks_failed": chunks_failed,
            "cross_chunk_review_count": stage_c_result.cross_chunk_review_count,
            "target_cards_per_module_present": stage_c_result.target_cards_per_module_present,
            "target_quizzes_per_module_present": stage_c_result.target_quizzes_per_module_present,
        }

        all_failed = chunks_attempted > 0 and chunks_succeeded == 0
        if all_failed:
            error = {
                "type": "AllChunksFailed",
                "message": f"all {chunks_attempted} module_identify chunks failed",
                "chunks_failed": chunks_failed,
            }
            await self._run_state.fail_step(
                step_id,
                error_code=ErrorCode.IDENTIFY_FAILED.value,
                error_message=error["message"],
                error=error,
            )
            await self._session.commit()
            return (
                False,
                0,
                StageOutcome(stage=STAGE_MODULE_IDENTIFY, status="failed", error=error),
            )

        if emitted_candidates == 0:
            error = {
                "type": "NoCandidatesIdentified",
                "message": "zero candidates were identified",
                "code": ErrorCode.IDENTIFY_NO_CANDIDATES.value,
            }
            await self._run_state.fail_step(
                step_id,
                error_code=ErrorCode.IDENTIFY_NO_CANDIDATES.value,
                error_message=error["message"],
                error=error,
            )
            await self._session.commit()
            return (
                False,
                0,
                StageOutcome(stage=STAGE_MODULE_IDENTIFY, status="failed", error=error),
            )

        await self._run_state.complete_step(step_id, output_summary=summary)
        await self._session.commit()  # persist candidates before D starts
        return (
            True,
            emitted_candidates,
            StageOutcome(stage=STAGE_MODULE_IDENTIFY, status="succeeded", summary=summary),
        )


class DraftStageRunner:
    """Stage D — draft cards/modules for each candidate."""

    def __init__(
        self,
        session: AsyncSession,
        run_state: RunStateService,
        candidate_repo: ModuleCandidateRepository,
    ) -> None:
        self._session = session
        self._run_state = run_state
        self._candidate_repo = candidate_repo

    async def run(
        self,
        *,
        stage_d: StageDOrchestrator,
        run_id: UUID,
        stages: list[StageOutcome],
    ) -> tuple[int, int]:
        """Run Stage D for each candidate. Returns (drafts_produced, failures)."""
        candidates: list[ModuleCandidateDraft] = await self._candidate_repo.list_candidates_for_run(run_id)
        # Snapshot primary keys before the loop. A rollback inside the
        # per-candidate failure path expires *every* attached ORM object,
        # so accessing `cand.id` on later iterations would trigger an
        # implicit DB reload outside greenlet context (MissingGreenlet).
        candidate_ids: list[UUID] = [cand.id for cand in candidates]
        drafts_produced = 0
        failures = 0
        for cand_id in candidate_ids:
            input_match = {"candidate_id": str(cand_id)}
            if await self._run_state.is_stage_succeeded(
                run_id, stage=STAGE_CARD_DRAFT, input_match=input_match
            ):
                drafts_produced += 1
                continue
            step = await self._run_state.start_step(
                run_id=run_id,
                stage=STAGE_CARD_DRAFT,
                input_summary=input_match,
            )
            step_id = step.id  # capture before commit (see Stage A comment)
            await self._session.commit()  # mark stage as 'running' for observers
            try:
                stage_d_result = await stage_d.run(
                    candidate_id=cand_id,
                    step_id=step_id,
                )
            except Exception as exc:
                logger.exception("Stage D failed for candidate %s", cand_id)
                # Roll back any partial Stage D state for this candidate
                # (otherwise SQLAlchemy stays in a poisoned txn) and record
                # the failure on a fresh transaction.
                await self._session.rollback()
                error = {
                    "candidate_id": str(cand_id),
                    "type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
                await self._run_state.fail_step(
                    step_id,
                    error_code=ErrorCode.DRAFT_FAILED.value,
                    error_message=str(exc)[:500],
                    error=error,
                )
                await self._session.commit()
                stages.append(StageOutcome(stage=STAGE_CARD_DRAFT, status="failed", error=error))
                failures += 1
                continue
            summary = {
                "candidate_id": str(cand_id),
                "module_id": str(stage_d_result.module_id) if stage_d_result.module_id else None,
                "secondary_module_id": (
                    str(stage_d_result.secondary_module_id) if stage_d_result.secondary_module_id else None
                ),
                "cards_count": stage_d_result.cards_count,
                "questions_count": stage_d_result.questions_count,
                "insufficient_reason": stage_d_result.insufficient_reason,
                "was_published_merge": stage_d_result.was_published_merge,
                "merged_from_module_id": (
                    str(stage_d_result.merged_from_module_id)
                    if stage_d_result.merged_from_module_id
                    else None
                ),
            }
            await self._run_state.complete_step(step_id, output_summary=summary)
            await self._session.commit()  # persist this candidate's draft module
            stages.append(StageOutcome(stage=STAGE_CARD_DRAFT, status="succeeded", summary=summary))
            if stage_d_result.module_id is not None:
                drafts_produced += 1
        return drafts_produced, failures
