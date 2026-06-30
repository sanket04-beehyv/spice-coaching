"""Per-stage runners — call stage services and return outcomes.

Resume/skip policy and SSE event emission stay in ``PipelineOrchestrator``;
these runners own step lifecycle (start → execute → complete/fail) for one
stage invocation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.run_state_service import (
    STAGE_CARD_DRAFT,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
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
            # Typed: outline_empty / no_pages / etc. Caller can branch on
            # error_jsonb.reason instead of parsing the message string.
            logger.error(
                "Stage 1 contract violation for source_document %s: %s",
                source_document_id,
                exc,
            )
            await self._session.rollback()
            error = {
                "type": "Stage1ExtractionError",
                "reason": "outline_empty",
                "message": str(exc)[:500],
            }
            await self._run_state.fail_step(step_id, error=error)
            await self._session.commit()
            return False, StageOutcome(stage=STAGE_EXTRACT, status="failed", error=error)
        except Exception as exc:
            logger.exception("Stage 1 crashed for source_document %s", source_document_id)
            await self._session.rollback()
            error = {"type": type(exc).__name__, "message": str(exc)[:500]}
            await self._run_state.fail_step(step_id, error=error)
            await self._session.commit()
            return False, StageOutcome(stage=STAGE_EXTRACT, status="failed", error=error)
        summary = {
            "total_pages": stage_a_result.total_pages,
            "pages_persisted": stage_a_result.pages_persisted,
            "extraction_method_counts": stage_a_result.extraction_method_counts,
            # Outline assembly is now part of Stage 1; surface the section
            # count in the step summary so an empty-outline failure (caught
            # by Stage1ExtractionError above) is observable in run history.
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
    ) -> tuple[bool, int, StageOutcome]:
        source_repo = SourceRepository(self._session)
        source_doc = await source_repo.get_source_document(source_document_id)
        ingestion_instructions_present = bool(source_doc and source_doc.ingestion_instructions)
        step = await self._run_state.start_step(
            run_id=run_id,
            stage=STAGE_MODULE_IDENTIFY,
            input_summary={
                "source_document_ids": [str(source_document_id)],
                "ingestion_instructions_present": ingestion_instructions_present,
            },
        )
        step_id = step.id  # capture before commit (see Stage A comment)
        await self._session.commit()  # mark stage as 'running' for observers
        try:
            stage_c_result = await stage_c.run(
                ingestion_run_id=run_id,
                source_document_ids=[source_document_id],
            )
        except Exception as exc:
            logger.exception("Stage C failed for run %s", run_id)
            await self._session.rollback()
            error = {"type": type(exc).__name__, "message": str(exc)[:500]}
            await self._run_state.fail_step(step_id, error=error)
            await self._session.commit()
            return (
                False,
                0,
                StageOutcome(stage=STAGE_MODULE_IDENTIFY, status="failed", error=error),
            )
        summary = {
            "candidates_emitted": stage_c_result.candidates_emitted,
            "candidates_flagged": stage_c_result.candidates_flagged,
            "flag_counts": stage_c_result.flag_counts,
            "chunks_attempted": stage_c_result.chunks_attempted,
            "chunks_succeeded": stage_c_result.chunks_succeeded,
            "chunks_failed": stage_c_result.chunks_failed,
            "cross_chunk_review_count": stage_c_result.cross_chunk_review_count,
        }
        await self._run_state.complete_step(step_id, output_summary=summary)
        await self._session.commit()  # persist candidates before D starts
        return (
            True,
            stage_c_result.candidates_emitted,
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
        skip_merge: bool,
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
                    skip_merge=skip_merge,
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
                await self._run_state.fail_step(step_id, error=error)
                await self._session.commit()
                stages.append(StageOutcome(stage=STAGE_CARD_DRAFT, status="failed", error=error))
                failures += 1
                continue
            summary = {
                "candidate_id": str(cand_id),
                "module_id": str(stage_d_result.module_id) if stage_d_result.module_id else None,
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
