"""W-7 — pipeline orchestrator.

Drives one source_document through Stages A → B → C → D, persisting
ingestion_run + ingestion_run_step state at every transition. Designed to
be:

- **Resumable**: re-running on a partially-failed run skips stages whose
  step row is already 'succeeded'. LLM-bound stages (B/C/D) additionally
  hit `llm_call_cache` for cheap re-execution, so even when a step row is
  missing or failed, the underlying ai-runtime calls aren't repeated.
- **Concurrency-safe**: refuses to start a second run for the same
  source_document while one is already 'running'.
- **Failure-isolating**: a failing stage marks the run partially_succeeded
  rather than failed — downstream stages are skipped, but the run remains
  resumable. A run is marked `failed` only when Stage A (the entry point)
  blows up.

We deliberately don't pull in LangGraph: the pipeline is linear with no
real branching. Per-candidate Stage D parallelism is handled in-process
via sequential awaits (deterministic ordering for tests; pilot corpora
are small enough that parallelism doesn't matter yet).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.base import SessionLocal
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.card_drafter import CardDrafter
from platform_service.services.llm_call_cache_service import CachingAIRuntimeClient
from platform_service.services.module_identifier import ModuleIdentifier
from platform_service.services.run_state_service import (
    RUN_FAILED,
    RUN_PARTIALLY_SUCCEEDED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    STAGE_CARD_DRAFT,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    ConcurrentRunError,
    RunStateService,
)
from platform_service.services.source_path_materialize import materialize_local_source_file
from platform_service.workers.extractors.vision_extractor import VisionExtractor
from platform_service.workers.pipeline.stage_runner import (
    DraftStageRunner,
    ExtractStageRunner,
    IdentifyStageRunner,
)
from platform_service.workers.pipeline.types import PipelineResult, StageOutcome
from platform_service.workers.stage_a_extract import StageAExtractor
from platform_service.workers.stage_c_identify import StageCOrchestrator
from platform_service.workers.stage_d_draft import StageDOrchestrator

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """Single entry point for one-document pipeline execution."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        ai_client: AIRuntimeClient | CachingAIRuntimeClient | None = None,
        stage_a: StageAExtractor | None = None,
        stage_c: StageCOrchestrator | None = None,
        stage_d: StageDOrchestrator | None = None,
    ) -> None:
        self._session = session
        self._run_state = RunStateService(session)
        self._candidate_repo = ModuleCandidateRepository(session)

        # Caching wrapper: every LLM-bound stage we instantiate by default
        # gets the cache so resume-from-cache works without per-stage opt-in.
        # Tests can pass their own ai_client (or a fully-mocked stage)
        # to bypass.
        if ai_client is None:
            ai_client = CachingAIRuntimeClient(session=session)
        self._ai_client = ai_client

        # Stages — accept overrides for test injection. Outline assembly
        # was a separate Stage B; per the architecture reset it is folded
        # into Stage 1 (`StageAExtractor` writes outline_jsonb at the tail
        # of its run and fails the stage if the outline is empty).
        self._stage_a = stage_a or StageAExtractor(
            session,
            vision_extractor=VisionExtractor(client=ai_client),
            ai_client=ai_client if isinstance(ai_client, AIRuntimeClient) else ai_client.inner,
        )
        self._stage_c = stage_c or StageCOrchestrator(
            session,
            identifier=ModuleIdentifier(client=ai_client),
        )
        self._stage_d = stage_d or StageDOrchestrator(
            session,
            card_drafter=CardDrafter(client=ai_client),
        )

        self._extract_runner = ExtractStageRunner(session, self._run_state)
        self._identify_runner = IdentifyStageRunner(session, self._run_state)
        self._draft_runner = DraftStageRunner(session, self._run_state, self._candidate_repo)

    def _clone_for_session(self, session: AsyncSession) -> PipelineOrchestrator:
        """Build a stage-scoped orchestrator that shares the same httpx client."""
        if isinstance(self._ai_client, CachingAIRuntimeClient):
            ai_client: AIRuntimeClient | CachingAIRuntimeClient = CachingAIRuntimeClient(
                session=session,
                inner=self._ai_client.inner,
            )
        else:
            ai_client = self._ai_client
        return PipelineOrchestrator(session, ai_client=ai_client)

    @asynccontextmanager
    async def _stage_context(self, staged_sessions: bool) -> AsyncIterator[PipelineOrchestrator]:
        if staged_sessions:
            async with SessionLocal() as session:
                yield self._clone_for_session(session)
        else:
            yield self

    @classmethod
    async def run_staged(cls, **kwargs: Any) -> PipelineResult:
        """Run the pipeline releasing the DB connection between stages."""
        async with SessionLocal() as session:
            driver = cls(session)
        return await driver.run(**kwargs, staged_sessions=True)

    # ── Entry ───────────────────────────────────────────────────────────

    async def run(
        self,
        *,
        source_document_id: UUID,
        source_path: str | Path,
        source_type: str,
        primary_language: str = "bn",
        triggered_by: UUID | None = None,
        resume: bool = True,
        skip_merge: bool = False,
        staged_sessions: bool = False,
    ) -> PipelineResult:
        """Execute the full A→B→C→D pipeline for one source document.

        If `resume=True` and a partially-failed run exists for this document,
        we attach to that run and skip already-succeeded stages.

        When ``staged_sessions=True``, each pipeline stage uses its own DB
        session so long-running LLM work does not hold a connection for hours.
        """
        result_box: list[PipelineResult] = []
        async for _ in self._drive_pipeline(
            source_document_id=source_document_id,
            source_path=source_path,
            source_type=source_type,
            primary_language=primary_language,
            triggered_by=triggered_by,
            resume=resume,
            skip_merge=skip_merge,
            staged_sessions=staged_sessions,
            emit_events=False,
            result_box=result_box,
        ):
            pass
        return result_box[0]

    async def run_generator(
        self,
        *,
        source_document_id: UUID,
        source_path: str | Path,
        source_type: str,
        primary_language: str = "bn",
        triggered_by: UUID | None = None,
        resume: bool = True,
        skip_merge: bool = False,
        staged_sessions: bool = False,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Like run(), but yields SSE-style progress dicts at each stage transition.

        Each yielded dict has at minimum:
          {"event": <str>, "run_id": <str>, ...}

        Events emitted:
          run_started       — initial run row created
          stage_started     — a stage has begun
          stage_succeeded   — a stage completed successfully
          stage_skipped     — a stage was skipped (resume)
          stage_failed      — a stage failed
          pipeline_complete — final event with full PipelineResult summary
        """
        async for event in self._drive_pipeline(
            source_document_id=source_document_id,
            source_path=source_path,
            source_type=source_type,
            primary_language=primary_language,
            triggered_by=triggered_by,
            resume=resume,
            skip_merge=skip_merge,
            staged_sessions=staged_sessions,
            emit_events=True,
            result_box=None,
        ):
            yield event

    async def _drive_pipeline(
        self,
        *,
        source_document_id: UUID,
        source_path: str | Path,
        source_type: str,
        primary_language: str,
        triggered_by: UUID | None,
        resume: bool,
        skip_merge: bool,
        staged_sessions: bool,
        emit_events: bool,
        result_box: list[PipelineResult] | None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Single A→identify→draft state machine; optionally yields SSE progress dicts."""
        run_id: UUID
        run_id_str: str
        claim_token = str(uuid.uuid4())

        async with self._stage_context(staged_sessions) as orch:
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
                await orch._session.commit()  # surface the run to polling endpoints

            if not await orch._run_state.try_claim_run(run.id, claim_token=claim_token):
                raise ConcurrentRunError(source_document_id, run.id)
            await orch._session.commit()

            run_id = run.id
            run_id_str = str(run_id)
            result = PipelineResult(
                run_id=run_id,
                source_document_id=source_document_id,
                final_status=RUN_SUCCEEDED,
            )
            if result_box is not None:
                result_box.append(result)

            if emit_events:
                yield {
                    "event": "run_started",
                    "run_id": run_id_str,
                    "source_document_id": str(source_document_id),
                }

        local_source_path, staged_cleanup = await materialize_local_source_file(source_path)
        try:
            if emit_events:
                yield {"event": "stage_started", "run_id": run_id_str, "stage": STAGE_EXTRACT}
            async with self._stage_context(staged_sessions) as orch:
                await orch._run_state.refresh_run_claim(run_id, claim_token=claim_token)
                ok = await orch._run_extract(
                    result=result,
                    run_id=run_id,
                    source_document_id=source_document_id,
                    source_path=local_source_path,
                    source_type=source_type,
                    primary_language=primary_language,
                )
            outcome_a = result.stages[-1]
            if emit_events:
                progress = self._stage_progress_event(run_id_str, STAGE_EXTRACT, outcome_a, ok=ok)
                if progress is not None:
                    yield progress
            if not ok:
                async with self._stage_context(staged_sessions) as orch:
                    await orch._run_state.complete_run(
                        run_id,
                        status=RUN_FAILED,
                        error_jsonb={"failed_stage": STAGE_EXTRACT},
                    )
                    await orch._session.commit()
                result.final_status = RUN_FAILED
                if emit_events:
                    yield {
                        "event": "pipeline_complete",
                        "run_id": run_id_str,
                        "final_status": result.final_status,
                    }
                return

            if emit_events:
                yield {
                    "event": "stage_started",
                    "run_id": run_id_str,
                    "stage": STAGE_MODULE_IDENTIFY,
                }
            async with self._stage_context(staged_sessions) as orch:
                await orch._run_state.refresh_run_claim(run_id, claim_token=claim_token)
                ok, candidates_emitted = await orch._run_identify(
                    result=result,
                    run_id=run_id,
                    source_document_id=source_document_id,
                )
            outcome_c = result.stages[-1]
            if emit_events:
                progress = self._stage_progress_event(run_id_str, STAGE_MODULE_IDENTIFY, outcome_c, ok=ok)
                if progress is not None:
                    yield progress
            if not ok:
                async with self._stage_context(staged_sessions) as orch:
                    await orch._run_state.complete_run(
                        run_id,
                        status=RUN_PARTIALLY_SUCCEEDED,
                        error_jsonb={"failed_stage": STAGE_MODULE_IDENTIFY},
                    )
                    await orch._session.commit()
                result.final_status = RUN_PARTIALLY_SUCCEEDED
                if emit_events:
                    yield {
                        "event": "pipeline_complete",
                        "run_id": run_id_str,
                        "final_status": result.final_status,
                    }
                return
            result.candidates_emitted = candidates_emitted

            if emit_events:
                yield {"event": "stage_started", "run_id": run_id_str, "stage": STAGE_CARD_DRAFT}
            if candidates_emitted == 0:
                async with self._stage_context(staged_sessions) as orch:
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
                if emit_events:
                    yield {
                        "event": "stage_skipped",
                        "run_id": run_id_str,
                        "stage": STAGE_CARD_DRAFT,
                        "reason": "no_candidates_from_stage_c",
                    }
                    if result.final_status != RUN_RUNNING:
                        yield {
                            "event": "pipeline_complete",
                            "run_id": run_id_str,
                            "final_status": result.final_status,
                            "candidates_emitted": 0,
                            "drafts_produced": 0,
                        }
                return

            async with self._stage_context(staged_sessions) as orch:
                await orch._run_state.refresh_run_claim(run_id, claim_token=claim_token)
                drafts_produced, draft_failures = await orch._run_drafting(
                    result=result,
                    run_id=run_id,
                    skip_merge=skip_merge,
                )
            result.drafts_produced = drafts_produced
            if emit_events:
                outcome_d = result.stages[-1]
                if outcome_d.status == "skipped":
                    yield {
                        "event": "stage_skipped",
                        "run_id": run_id_str,
                        "stage": STAGE_CARD_DRAFT,
                    }
                elif draft_failures > 0:
                    yield {
                        "event": "stage_failed",
                        "run_id": run_id_str,
                        "stage": STAGE_CARD_DRAFT,
                        "error": outcome_d.error,
                    }
                else:
                    yield {
                        "event": "stage_succeeded",
                        "run_id": run_id_str,
                        "stage": STAGE_CARD_DRAFT,
                        "summary": outcome_d.summary,
                    }

            async with self._stage_context(staged_sessions) as orch:
                await orch._run_state.maybe_finalize_ingestion_run(run_id)
                await orch._session.commit()
                refreshed = await orch._run_state.get_run(run_id)
            result.final_status = refreshed.status if refreshed is not None else RUN_RUNNING
            if emit_events and result.final_status != RUN_RUNNING:
                yield {
                    "event": "pipeline_complete",
                    "run_id": run_id_str,
                    "final_status": result.final_status,
                    "candidates_emitted": result.candidates_emitted,
                    "drafts_produced": result.drafts_produced,
                }
        finally:
            try:
                async with self._stage_context(staged_sessions) as orch:
                    await orch._run_state.release_run_claim(run_id, claim_token=claim_token)
                    await orch._session.commit()
            except Exception:
                logger.exception(
                    "Failed to release pipeline claim for run_id=%s",
                    run_id,
                )
            if staged_cleanup is not None:
                staged_cleanup.unlink(missing_ok=True)

    @staticmethod
    def _stage_progress_event(
        run_id_str: str,
        stage: str,
        outcome: StageOutcome,
        *,
        ok: bool,
    ) -> dict[str, Any] | None:
        if outcome.status == "skipped":
            return {"event": "stage_skipped", "run_id": run_id_str, "stage": stage}
        if ok:
            return {
                "event": "stage_succeeded",
                "run_id": run_id_str,
                "stage": stage,
                "summary": outcome.summary,
            }
        return {
            "event": "stage_failed",
            "run_id": run_id_str,
            "stage": stage,
            "error": outcome.error,
        }

    # ── Stage runners ───────────────────────────────────────────────────

    async def _run_extract(
        self,
        *,
        result: PipelineResult,
        run_id: UUID,
        source_document_id: UUID,
        source_path: str | Path,
        source_type: str,
        primary_language: str,
    ) -> bool:
        if await self._run_state.is_stage_succeeded(run_id, stage=STAGE_EXTRACT):
            logger.info("Resume: skipping Stage A (already succeeded)")
            result.stages.append(StageOutcome(stage=STAGE_EXTRACT, status="skipped"))
            return True
        ok, outcome = await self._extract_runner.run(
            stage_a=self._stage_a,
            run_id=run_id,
            source_document_id=source_document_id,
            source_path=source_path,
            source_type=source_type,
            primary_language=primary_language,
        )
        result.stages.append(outcome)
        return ok

    async def _run_identify(
        self,
        *,
        result: PipelineResult,
        run_id: UUID,
        source_document_id: UUID,
    ) -> tuple[bool, int]:
        if await self._run_state.is_stage_succeeded(run_id, stage=STAGE_MODULE_IDENTIFY):
            logger.info("Resume: skipping Stage C (already succeeded)")
            existing = await self._candidate_repo.list_candidates_for_run(run_id)
            result.stages.append(
                StageOutcome(
                    stage=STAGE_MODULE_IDENTIFY,
                    status="skipped",
                    summary={"existing_candidates": len(existing)},
                )
            )
            return True, len(existing)
        ok, candidates_emitted, outcome = await self._identify_runner.run(
            stage_c=self._stage_c,
            run_id=run_id,
            source_document_id=source_document_id,
        )
        result.stages.append(outcome)
        return ok, candidates_emitted

    async def _run_drafting(
        self,
        *,
        result: PipelineResult,
        run_id: UUID,
        skip_merge: bool = False,
    ) -> tuple[int, int]:
        """Run Stage D for each candidate. Returns (drafts_produced, failures)."""
        return await self._draft_runner.run(
            stage_d=self._stage_d,
            run_id=run_id,
            skip_merge=skip_merge,
            stages=result.stages,
        )
