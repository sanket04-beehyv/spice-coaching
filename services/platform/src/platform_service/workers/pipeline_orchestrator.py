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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.card_drafter import CardDrafter
from platform_service.services.llm_call_cache_service import CachingAIRuntimeClient
from platform_service.services.module_identifier import ModuleIdentifier
from platform_service.services.run_state_service import (
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    RunStateService,
)
from platform_service.workers.extractors.vision_extractor import VisionExtractor
from platform_service.workers.pipeline.pipeline_driver import drive_pipeline
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
        primary_language: str | None = None,
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
        resolved_primary_language = primary_language or get_settings().deployment_primary_locale
        result_box: list[PipelineResult] = []
        await drive_pipeline(
            self,
            source_document_id=source_document_id,
            source_path=source_path,
            source_type=source_type,
            primary_language=resolved_primary_language,
            triggered_by=triggered_by,
            resume=resume,
            skip_merge=skip_merge,
            staged_sessions=staged_sessions,
            result_box=result_box,
        )
        return result_box[0]

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
