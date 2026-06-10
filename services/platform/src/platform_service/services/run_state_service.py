"""W-7 — pipeline run state service.

Manages the ingestion_run + ingestion_run_step rows that track each pipeline
pass. The orchestrator (`workers/pipeline_orchestrator.py`) calls into this
service at every transition so that:
- Resume-after-failure can find the last completed step and skip ahead.
- Concurrent runs on the same source_document are rejected at start.
- Operators can query run/step status via admin or telemetry tooling.

The tables are staging-only (30-day retention per Pipeline §16 P3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep

# Canonical stage names used as ingestion_run_step.stage values.
# Outline assembly was its own stage in v3.3 (`outline`); per the
# architecture reset it is folded into Stage 1 (`extract`), so the canonical
# set is three stages, not four. Per-candidate Stage 2-draft rows carry
# input_summary={"candidate_id":...}.
STAGE_EXTRACT = "extract"
STAGE_MODULE_IDENTIFY = "module_identify"
STAGE_CARD_DRAFT = "card_draft"
STAGE_QUIZ_GENERATION = "quiz_generation"
STAGE_EMBEDDING_GENERATION = "embedding_generation"
STAGE_GAP_CLASSIFICATION = "gap_classification"
STAGE_CROSS_SOURCE_FUSION = "cross_source_fusion"

PIPELINE_STAGES = (STAGE_EXTRACT, STAGE_MODULE_IDENTIFY, STAGE_CARD_DRAFT)
POST_PUBLISH_STAGES = (
    STAGE_QUIZ_GENERATION,
    STAGE_EMBEDDING_GENERATION,
    STAGE_GAP_CLASSIFICATION,
)
ALL_STAGES = PIPELINE_STAGES + POST_PUBLISH_STAGES + (STAGE_CROSS_SOURCE_FUSION,)

# ingestion_run.error_jsonb["type"] for cross-source fusion runs
FUSION_RUN_TYPE = "cross_source_fusion"

# Run statuses
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_PARTIALLY_SUCCEEDED = "partially_succeeded"

# Step statuses
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

_TERMINAL_STEP_STATUSES = frozenset({STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED})

# Stored in ingestion_run.error_jsonb under this key while a worker drives the run.
_PIPELINE_CLAIM_KEY = "_pipeline_claim"
# Ingest jobs may run up to 4h; stale claims allow takeover after worker death.
_DEFAULT_CLAIM_STALE_SECONDS = 6 * 60 * 60


class ConcurrentRunError(Exception):
    """Raised when starting a new run while another is already running for
    the same source_document_id."""

    def __init__(self, source_document_id: UUID, existing_run_id: UUID) -> None:
        super().__init__(
            f"source_document {source_document_id} already has an active run "
            f"({existing_run_id}); refuse to start a second concurrent run"
        )
        self.source_document_id = source_document_id
        self.existing_run_id = existing_run_id


class ConcurrentFusionRunError(Exception):
    """Raised when a fusion run is already active for an overlapping document set."""

    def __init__(self, source_document_id: UUID, existing_run_id: UUID) -> None:
        super().__init__(
            f"source_document {source_document_id} already participates in an active "
            f"fusion run ({existing_run_id}); refuse to start a second concurrent fusion"
        )
        self.source_document_id = source_document_id
        self.existing_run_id = existing_run_id


def _now() -> datetime:
    return datetime.now(UTC)


class RunStateService:
    """Persists run + step state for the pipeline orchestrator."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Run lifecycle ───────────────────────────────────────────────────

    async def find_active_run(self, source_document_id: UUID) -> IngestionRun | None:
        """Return the running ingestion_run for this source_document, if any."""
        result = await self._session.execute(
            select(IngestionRun)
            .where(
                IngestionRun.source_document_id == source_document_id,
                IngestionRun.status == RUN_RUNNING,
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def find_active_fusion_run_for_document(self, source_document_id: UUID) -> IngestionRun | None:
        """Return a running cross-source fusion run that includes this source_document."""
        doc_str = str(source_document_id)
        result = await self._session.execute(
            select(IngestionRun)
            .where(
                IngestionRun.status == RUN_RUNNING,
                IngestionRun.error_jsonb["type"].astext == FUSION_RUN_TYPE,
                IngestionRun.error_jsonb["source_document_ids"].contains([doc_str]),
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _lock_source_document(self, source_document_id: UUID) -> None:
        """Serialize run creation per source_document within this transaction."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:doc_id))"),
            {"doc_id": str(source_document_id)},
        )

    async def assert_no_active_fusion_overlap(self, source_document_ids: list[UUID]) -> None:
        """Reject fusion when any constituent already has an active fusion run."""
        for doc_id in source_document_ids:
            active_ingest = await self.find_active_run(doc_id)
            if active_ingest is not None:
                raise ConcurrentRunError(doc_id, active_ingest.id)
            active_fusion = await self.find_active_fusion_run_for_document(doc_id)
            if active_fusion is not None:
                raise ConcurrentFusionRunError(doc_id, active_fusion.id)

    async def start_run(
        self,
        *,
        source_document_id: UUID,
        triggered_by: UUID | None = None,
    ) -> IngestionRun:
        """Create a fresh ingestion_run.

        Raises ConcurrentRunError if a run is already active for this document.
        Edge case (Pipeline W-7 §1, §7): two simultaneous starts ⇒ second fails.
        """
        await self._lock_source_document(source_document_id)
        existing = await self.find_active_run(source_document_id)
        if existing is not None:
            raise ConcurrentRunError(source_document_id, existing.id)
        run = IngestionRun(
            source_document_id=source_document_id,
            status=RUN_RUNNING,
            triggered_by=triggered_by,
        )
        self._session.add(run)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            existing = await self.find_active_run(source_document_id)
            if existing is not None:
                raise ConcurrentRunError(source_document_id, existing.id) from exc
            raise
        return run

    async def start_fusion_run(
        self,
        *,
        source_document_ids: list[UUID],
    ) -> IngestionRun:
        """Create a cross-source fusion ingestion_run with overlap checks."""
        if not source_document_ids:
            raise ValueError("source_document_ids must not be empty")
        anchor = source_document_ids[0]
        await self._lock_source_document(anchor)
        await self.assert_no_active_fusion_overlap(source_document_ids)
        run = IngestionRun(
            source_document_id=anchor,
            status=RUN_RUNNING,
            error_jsonb={
                "type": FUSION_RUN_TYPE,
                "source_document_ids": [str(d) for d in source_document_ids],
            },
        )
        self._session.add(run)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            existing = await self.find_active_run(anchor)
            if existing is not None:
                raise ConcurrentRunError(anchor, existing.id) from exc
            raise
        return run

    async def get_run(self, run_id: UUID) -> IngestionRun | None:
        return await self._session.get(IngestionRun, run_id)

    async def complete_run(
        self,
        run_id: UUID,
        *,
        status: str,
        error_jsonb: dict[str, Any] | None = None,
    ) -> IngestionRun:
        """Finalize a run with succeeded / failed / partially_succeeded status."""
        if status not in (RUN_SUCCEEDED, RUN_FAILED, RUN_PARTIALLY_SUCCEEDED):
            raise ValueError(f"invalid terminal run status: {status!r}")
        run = await self.get_run(run_id)
        if run is None:
            raise ValueError(f"ingestion_run {run_id} not found")
        run.status = status
        run.completed_at = _now()
        if error_jsonb is not None:
            run.error_jsonb = error_jsonb
        await self._session.flush()
        return run

    async def find_resumable_run(self, source_document_id: UUID) -> IngestionRun | None:
        """Find a run for this document we should resume from rather than starting fresh.

        Picks the most recent run that is still 'running' (worker died mid-stage)
        or 'partially_succeeded' (Stage X failed, downstream not attempted).
        """
        result = await self._session.execute(
            select(IngestionRun)
            .where(
                IngestionRun.source_document_id == source_document_id,
                IngestionRun.status.in_((RUN_RUNNING, RUN_PARTIALLY_SUCCEEDED)),
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def try_claim_run(
        self,
        run_id: UUID,
        *,
        claim_token: str,
        stale_after_seconds: int = _DEFAULT_CLAIM_STALE_SECONDS,
    ) -> bool:
        """Atomically claim a resumable run for this worker.

        Returns False when another worker holds a fresh claim. Stale claims
        (no heartbeat refresh within ``stale_after_seconds``) may be taken over.
        """
        now = _now()
        stale_before = (now - timedelta(seconds=stale_after_seconds)).isoformat()
        result = await self._session.execute(
            text("""
                UPDATE ingestion_run
                SET error_jsonb = COALESCE(error_jsonb, '{}'::jsonb)
                    || jsonb_build_object(
                        :claim_key,
                        jsonb_build_object(
                            'claim_token', :claim_token,
                            'claimed_at', :claimed_at
                        )
                    )
                WHERE id = :run_id
                  AND status IN ('running', 'partially_succeeded')
                  AND (
                    error_jsonb IS NULL
                    OR error_jsonb->:claim_key IS NULL
                    OR error_jsonb->:claim_key->>'claim_token' = :claim_token
                    OR error_jsonb->:claim_key->>'claimed_at' < :stale_before
                  )
                RETURNING id
            """),
            {
                "run_id": run_id,
                "claim_key": _PIPELINE_CLAIM_KEY,
                "claim_token": claim_token,
                "claimed_at": now.isoformat(),
                "stale_before": stale_before,
            },
        )
        return result.scalar_one_or_none() is not None

    async def refresh_run_claim(self, run_id: UUID, *, claim_token: str) -> bool:
        """Extend the claim heartbeat while this worker is still driving the run."""
        now = _now()
        result = await self._session.execute(
            text("""
                UPDATE ingestion_run
                SET error_jsonb = COALESCE(error_jsonb, '{}'::jsonb)
                    || jsonb_build_object(
                        :claim_key,
                        jsonb_build_object(
                            'claim_token', :claim_token,
                            'claimed_at', :claimed_at
                        )
                    )
                WHERE id = :run_id
                  AND error_jsonb->:claim_key->>'claim_token' = :claim_token
                RETURNING id
            """),
            {
                "run_id": run_id,
                "claim_key": _PIPELINE_CLAIM_KEY,
                "claim_token": claim_token,
                "claimed_at": now.isoformat(),
            },
        )
        return result.scalar_one_or_none() is not None

    async def release_run_claim(self, run_id: UUID, *, claim_token: str) -> None:
        """Drop the pipeline claim when this worker finishes or aborts."""
        await self._session.execute(
            text("""
                UPDATE ingestion_run
                SET error_jsonb = error_jsonb - :claim_key
                WHERE id = :run_id
                  AND error_jsonb->:claim_key->>'claim_token' = :claim_token
            """),
            {
                "run_id": run_id,
                "claim_key": _PIPELINE_CLAIM_KEY,
                "claim_token": claim_token,
            },
        )

    @staticmethod
    def is_fusion_run(run: IngestionRun) -> bool:
        return (run.error_jsonb or {}).get("type") == FUSION_RUN_TYPE

    async def find_best_poll_run(self, source_document_id: UUID) -> IngestionRun | None:
        """Best pipeline run for ingest poll on a source_document.

        Prefers an active or resumable *pipeline* run (not cross-source fusion).
        Otherwise returns the most recent terminal pipeline run for the document.
        """
        run = await self.find_active_run(source_document_id)
        if run is not None and self.is_fusion_run(run):
            run = None
        if run is None:
            run = await self.find_resumable_run(source_document_id)
            if run is not None and self.is_fusion_run(run):
                run = None
        if run is not None:
            return run

        result = await self._session.execute(
            select(IngestionRun)
            .where(IngestionRun.source_document_id == source_document_id)
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        run = result.scalar_one_or_none()
        if run is None or not self.is_fusion_run(run):
            return run

        result = await self._session.execute(
            select(IngestionRun)
            .where(
                IngestionRun.source_document_id == source_document_id,
                IngestionRun.error_jsonb["type"].astext != FUSION_RUN_TYPE,
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ── Step lifecycle ──────────────────────────────────────────────────

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
        """Look up a step by stage and (optionally) input_summary contents.

        For per-candidate Stage D steps, pass input_match={"candidate_id": "..."}
        — we match if every key/value in input_match is present in the step's
        input_summary_jsonb.
        """
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
        """Create a new step row in 'running' status."""
        if stage not in ALL_STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        step = IngestionRunStep(
            ingestion_run_id=run_id,
            stage=stage,
            status=STEP_RUNNING,
            started_at=_now(),
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
        """Shallow-merge ``patch`` into a running step's input_summary_jsonb."""
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
        step.completed_at = _now()
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
        step.completed_at = _now()
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
        """Record that a stage was deliberately not run (e.g., Stage C produced
        zero candidates so per-candidate Stage D is skipped)."""
        if stage not in ALL_STAGES:
            raise ValueError(f"unknown stage: {stage!r}")
        step = IngestionRunStep(
            ingestion_run_id=run_id,
            stage=stage,
            status=STEP_SKIPPED,
            started_at=_now(),
            completed_at=_now(),
            input_summary_jsonb=input_summary,
            output_summary_jsonb={"skipped_reason": reason},
        )
        self._session.add(step)
        await self._session.flush()
        return step

    # ── Resume helpers ──────────────────────────────────────────────────

    async def is_stage_succeeded(
        self,
        run_id: UUID,
        *,
        stage: str,
        input_match: dict[str, Any] | None = None,
    ) -> bool:
        """True if the matching stage step for this run is in 'succeeded' state."""
        step = await self.find_step(run_id, stage=stage, input_match=input_match)
        return step is not None and step.status == STEP_SUCCEEDED

    # ── Post-publish finalization ───────────────────────────────────────

    async def maybe_finalize_ingestion_run(self, run_id: UUID) -> bool:
        """Finalize a run when all post-publish steps are terminal (or absent).

        Returns True if the run was finalized, False if it remains ``running``
        because post-publish work is still in flight. Safe to call from
        multiple Celery workers — only the first caller to see all terminal
        steps will transition the run out of ``running``.
        """
        run = await self.get_run(run_id)
        if run is None or run.status != RUN_RUNNING:
            return False

        all_steps = await self.list_steps(run_id)
        post_publish = [s for s in all_steps if s.stage in POST_PUBLISH_STAGES]

        if post_publish:
            if any(s.status not in _TERMINAL_STEP_STATUSES for s in post_publish):
                return False

        final_status, error_jsonb = _terminal_run_status_from_steps(all_steps)
        # Re-check run is still running (concurrent Celery workers).
        run = await self.get_run(run_id)
        if run is None or run.status != RUN_RUNNING:
            return False
        await self.complete_run(run_id, status=final_status, error_jsonb=error_jsonb)
        return True


def _terminal_run_status_from_steps(
    steps: list[IngestionRunStep],
) -> tuple[str, dict[str, Any] | None]:
    """Derive terminal run status from all step rows for a pipeline pass."""
    failed_stages: list[str] = []
    draft_failures = 0
    drafts_produced = 0

    for step in steps:
        if step.stage == STAGE_CARD_DRAFT:
            if step.status == STEP_FAILED:
                draft_failures += 1
            elif step.status == STEP_SUCCEEDED:
                drafts_produced += 1
        if step.status != STEP_FAILED:
            continue
        failed_stages.append(step.stage)

    if not failed_stages:
        return RUN_SUCCEEDED, None

    error: dict[str, Any] = {"failed_stages": failed_stages}
    if draft_failures:
        error["failed_stage"] = STAGE_CARD_DRAFT
        error["draft_failures"] = draft_failures
        error["drafts_produced"] = drafts_produced
    elif len(failed_stages) == 1:
        error["failed_stage"] = failed_stages[0]
    return RUN_PARTIALLY_SUCCEEDED, error
