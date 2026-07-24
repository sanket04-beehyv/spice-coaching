"""Layer 2 chunk 5 — pipeline orchestrator tests.

Critical regressions covered:

- **Greenlet bug** (the original P4 fix): each `_run_*` method captures
  `step_id = step.id` BEFORE any commit. Without that capture, the failure
  path's `await self._run_state.fail_step(step.id, ...)` would lazy-load
  `step.id` outside greenlet context. We assert no MissingGreenlet by
  forcing each stage's failure path.
- **Typed Stage1ExtractionError**: when Stage A raises
  `Stage1ExtractionError`, the orchestrator records
  `error_jsonb={'type': 'Stage1ExtractionError', 'reason': 'outline_empty', ...}`.
  Generic Stage A exceptions get type=<class name> only.
- **Stage A failure → run.status=failed** (not partially_succeeded — Stage 1
  is the precondition for everything else).
- **Stage 2 failure → run.status=partially_succeeded** (downstream skipped).
- **Stage D per-candidate skip-on-failure**: one bad candidate doesn't
  abort the whole run; surviving candidates still publish; final status
  reflects partial success.
- **Resume**: re-running picks up the existing run and skips
  already-succeeded stages.
- **Empty Stage C result**: zero candidates → Stage D skipped, run
  succeeded.

Tests use mock stage instances (StageAExtractor / StageCOrchestrator /
StageDOrchestrator) so the orchestrator's logic is exercised without
needing a real PDF, ai-runtime, or migration data beyond what
ingestion_run / ingestion_run_step needs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.run_state_service import (
    RUN_FAILED,
    RUN_PARTIALLY_SUCCEEDED,
    RUN_SUCCEEDED,
    STAGE_CARD_DRAFT,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    STEP_FAILED,
    RunStateService,
)
from platform_service.workers.extractors.calibration import build_calibration_decision
from platform_service.workers.pipeline_orchestrator import (
    PipelineOrchestrator,
)
from platform_service.workers.stage_a_extract import (
    Stage1ExtractionError,
    StageAExtractor,
    StageAResult,
)
from platform_service.workers.stage_c_identify import (
    StageCOrchestrator,
    StageCResult,
)
from platform_service.workers.stage_d_draft import StageDOrchestrator, StageDResult
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text(
            "TRUNCATE module_quiz_question, module, module_family, "
            "module_candidate_draft, content_block, source_page, "
            "source_document, ingestion_run_step, ingestion_run "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


# ─── Helpers ──────────────────────────────────────────────────────────────


async def _seed_source_document(session: AsyncSession) -> UUID:
    sd = SourceDocument(
        title="seed",
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        assessment_mode="with_quiz",
        original_storage_path="/tmp/x.pdf",
    )
    session.add(sd)
    await session.flush()
    await session.commit()
    return sd.id


def _stage_a_mock(*, success: bool = True, raise_exc: Exception | None = None) -> MagicMock:
    """Build a StageAExtractor mock. By default returns a successful
    StageAResult. Pass raise_exc to make it raise."""
    m = MagicMock(spec=StageAExtractor)
    if raise_exc:
        m.run = AsyncMock(side_effect=raise_exc)
    else:
        result = StageAResult(
            source_document_id=uuid4(),
            total_pages=10,
            pages_persisted=10,
            extraction_method_counts={"text": 10},
            calibration=build_calibration_decision(
                sample_pages=[1, 2, 3], sample_pass_count=3, sample_fail_count=0
            ),
            outline_section_count=4,
        )
        m.run = AsyncMock(return_value=result)
    return m


def _stage_c_mock(*, candidates_emitted: int = 1, raise_exc: Exception | None = None) -> MagicMock:
    m = MagicMock(spec=StageCOrchestrator)
    if raise_exc:
        m.run = AsyncMock(side_effect=raise_exc)
    else:
        m.run = AsyncMock(
            return_value=StageCResult(
                ingestion_run_id=uuid4(),
                candidates_emitted=candidates_emitted,
                candidates_flagged=0,
                flag_counts={},
                estimated_corpus_tokens=1000,
            )
        )
    return m


def _stage_d_mock(*, raise_for_candidate: dict[UUID, Exception] | None = None) -> MagicMock:
    """StageD mock returning a successful StageDResult by default. Pass a
    dict mapping candidate_id → exception for per-candidate failures."""
    m = MagicMock(spec=StageDOrchestrator)
    raise_for_candidate = raise_for_candidate or {}

    async def _run(candidate_id: UUID, **kwargs: object) -> StageDResult:
        if candidate_id in raise_for_candidate:
            raise raise_for_candidate[candidate_id]
        return StageDResult(
            candidate_id=candidate_id,
            module_id=uuid4(),
            cards_count=5,
            questions_count=0,
            insufficient_reason=None,
        )

    m.run = AsyncMock(side_effect=_run)
    return m


async def _seed_candidates(session: AsyncSession, ingestion_run_id: UUID, count: int = 1) -> list[UUID]:
    """Seed fake module_candidate_draft rows for Stage D. Returns IDs."""
    ids: list[UUID] = []
    for i in range(count):
        cand = ModuleCandidateDraft(
            ingestion_run_id=ingestion_run_id,
            proposed_title=f"T{i}",
            scope_summary="x",
            source_provenance_jsonb=[],
            estimated_card_count=5,
            estimated_quiz_count=4,
            proposed_module_type="refresher",
        )
        session.add(cand)
        await session.flush()
        ids.append(cand.id)
    await session.commit()
    return ids


# ─── Happy path ───────────────────────────────────────────────────────────


class TestHappyPath:
    async def test_all_stages_succeed_run_status_succeeded(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        stage_a = _stage_a_mock()
        stage_c = _stage_c_mock(candidates_emitted=2)
        stage_d = _stage_d_mock()

        orch = PipelineOrchestrator(db_session, stage_a=stage_a, stage_c=stage_c, stage_d=stage_d)

        # We need to fake the candidate rows that Stage C "would have created"
        # so Stage D's per-candidate loop has something to iterate. Approach:
        # call Stage C first to set up the run, then seed candidates manually.
        # Easier: monkey-patch the candidate_repo to return our seeded ones.
        # But the orchestrator constructs the repo in __init__ from session.
        # Instead: start the run via run() — Stage C mock returns a count,
        # but the orchestrator looks up actual candidates via the repo. So
        # the seeded count must MATCH the mock's emitted count for Stage D
        # to iterate.
        # We seed 0 here, set Stage C emitted=0, and assert run succeeds with
        # Stage D skipped (the alternate happy path).
        stage_c = _stage_c_mock(candidates_emitted=0)
        orch._stage_c = stage_c
        orch._stage_d = stage_d

        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
        )

        assert result.final_status == RUN_SUCCEEDED
        # Stage A run.
        stage_a.run.assert_awaited_once()
        # Stage C run.
        stage_c.run.assert_awaited_once()
        # Stage D was skipped (no candidates) — never called.
        stage_d.run.assert_not_awaited()


# ─── Stage A failure paths ────────────────────────────────────────────────


class TestStage1ExtractionFailures:
    async def test_outline_empty_records_typed_reason(self, db_session: AsyncSession) -> None:
        """The architecture-reset P4 fix: orchestrator catches
        Stage1ExtractionError specifically and writes
        error_jsonb={'type': 'Stage1ExtractionError', 'reason': 'outline_empty', ...}
        — a typed signal the dashboard can branch on without parsing the
        message string."""
        sd_id = await _seed_source_document(db_session)
        stage_a = _stage_a_mock(raise_exc=Stage1ExtractionError("outline empty after extraction (pages=10)"))

        orch = PipelineOrchestrator(
            db_session, stage_a=stage_a, stage_c=_stage_c_mock(), stage_d=_stage_d_mock()
        )
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
        )

        assert result.final_status == RUN_FAILED
        # The run row carries failed_stage in error_jsonb.
        run_row = (
            await db_session.execute(select(IngestionRun).where(IngestionRun.id == result.run_id))
        ).scalar_one()
        assert run_row.status == RUN_FAILED
        assert run_row.error_jsonb["failed_stage"] == STAGE_EXTRACT

        # The Stage A step row carries the typed reason.
        step = (
            await db_session.execute(
                select(IngestionRunStep)
                .where(IngestionRunStep.ingestion_run_id == result.run_id)
                .where(IngestionRunStep.stage == STAGE_EXTRACT)
            )
        ).scalar_one()
        assert step.status == STEP_FAILED
        err = step.error_jsonb
        assert err["type"] == "Stage1ExtractionError"
        assert err["reason"] == "outline_empty"
        assert "outline empty" in err["message"]

    async def test_generic_stage_a_exception_has_no_typed_reason(self, db_session: AsyncSession) -> None:
        """Non-Stage1ExtractionError exceptions go through the broad
        Exception handler and don't get the typed `reason` key."""
        sd_id = await _seed_source_document(db_session)
        stage_a = _stage_a_mock(raise_exc=ValueError("bad PDF"))

        orch = PipelineOrchestrator(
            db_session, stage_a=stage_a, stage_c=_stage_c_mock(), stage_d=_stage_d_mock()
        )
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
        )

        assert result.final_status == RUN_FAILED
        step = (
            await db_session.execute(
                select(IngestionRunStep)
                .where(IngestionRunStep.ingestion_run_id == result.run_id)
                .where(IngestionRunStep.stage == STAGE_EXTRACT)
            )
        ).scalar_one()
        err = step.error_jsonb
        assert err["type"] == "ValueError"
        assert "bad PDF" in err["message"]
        # No `reason` key on generic exceptions.
        assert "reason" not in err

    async def test_stage_a_failure_does_not_run_subsequent_stages(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        stage_a = _stage_a_mock(raise_exc=ValueError("boom"))
        stage_c = _stage_c_mock()
        stage_d = _stage_d_mock()

        orch = PipelineOrchestrator(db_session, stage_a=stage_a, stage_c=stage_c, stage_d=stage_d)
        await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
        )

        stage_a.run.assert_awaited_once()
        stage_c.run.assert_not_awaited()
        stage_d.run.assert_not_awaited()


# ─── Stage C failure paths ────────────────────────────────────────────────


class TestStageCFailures:
    async def test_stage_c_failure_marks_run_partially_succeeded(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        stage_c = _stage_c_mock(raise_exc=RuntimeError("Stage C blew up"))

        orch = PipelineOrchestrator(
            db_session,
            stage_a=_stage_a_mock(),
            stage_c=stage_c,
            stage_d=_stage_d_mock(),
        )
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
        )

        assert result.final_status == RUN_PARTIALLY_SUCCEEDED
        run_row = (
            await db_session.execute(select(IngestionRun).where(IngestionRun.id == result.run_id))
        ).scalar_one()
        assert run_row.status == RUN_PARTIALLY_SUCCEEDED
        assert run_row.error_jsonb["failed_stage"] == STAGE_MODULE_IDENTIFY


# ─── Stage D per-candidate failure ────────────────────────────────────────


class TestStageDPerCandidateFailures:
    async def test_one_failing_candidate_partially_succeeds(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        # Stage A + C succeed; Stage C reports 3 candidates. Then we seed 3
        # rows that the orchestrator's candidate_repo will iterate.
        # First start the run so we have an ingestion_run_id.
        run_state = RunStateService(db_session)
        run = await run_state.start_run(source_document_id=sd_id)
        await db_session.commit()
        candidate_ids = await _seed_candidates(db_session, run.id, count=3)

        stage_a = _stage_a_mock()
        stage_c = _stage_c_mock(candidates_emitted=3)
        # Make the middle candidate fail.
        stage_d = _stage_d_mock(raise_for_candidate={candidate_ids[1]: RuntimeError("bad cand")})

        orch = PipelineOrchestrator(db_session, stage_a=stage_a, stage_c=stage_c, stage_d=stage_d)
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
            resume=True,  # reuse the run we just created
        )

        assert result.final_status == RUN_PARTIALLY_SUCCEEDED
        # 2 of 3 candidates produced modules.
        assert result.drafts_produced == 2
        run_row = (
            await db_session.execute(select(IngestionRun).where(IngestionRun.id == result.run_id))
        ).scalar_one()
        assert run_row.error_jsonb["draft_failures"] == 1
        assert run_row.error_jsonb["drafts_produced"] == 2

    async def test_all_candidates_succeed_run_succeeded(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        run_state = RunStateService(db_session)
        run = await run_state.start_run(source_document_id=sd_id)
        await db_session.commit()
        await _seed_candidates(db_session, run.id, count=2)

        orch = PipelineOrchestrator(
            db_session,
            stage_a=_stage_a_mock(),
            stage_c=_stage_c_mock(candidates_emitted=2),
            stage_d=_stage_d_mock(),
        )
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
            resume=True,
        )

        assert result.final_status == RUN_SUCCEEDED
        assert result.drafts_produced == 2


# ─── Empty Stage C → Stage D skipped ──────────────────────────────────────


class TestEmptyStageC:
    async def test_zero_candidates_skips_stage_d_run_succeeds(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)
        stage_d = _stage_d_mock()

        orch = PipelineOrchestrator(
            db_session,
            stage_a=_stage_a_mock(),
            stage_c=_stage_c_mock(candidates_emitted=0),
            stage_d=stage_d,
        )
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
        )

        assert result.final_status == RUN_SUCCEEDED
        # Stage D never invoked.
        stage_d.run.assert_not_awaited()
        # And the card_draft step row exists with status='skipped'.
        step = (
            await db_session.execute(
                select(IngestionRunStep)
                .where(IngestionRunStep.ingestion_run_id == result.run_id)
                .where(IngestionRunStep.stage == STAGE_CARD_DRAFT)
            )
        ).scalar_one()
        assert step.status == "skipped"
        assert step.output_summary_jsonb["skipped_reason"] == "no_candidates_from_stage_c"


# ─── Resume behaviour ─────────────────────────────────────────────────────


class TestResume:
    async def test_resume_skips_already_succeeded_stage_a(self, db_session: AsyncSession) -> None:
        """If Stage A's step is already 'succeeded' on the resumable run,
        the orchestrator skips Stage A and continues from Stage C."""
        sd_id = await _seed_source_document(db_session)
        run_state = RunStateService(db_session)
        # Start a run with a completed extract step (worker interrupted before Stage C).
        run = await run_state.start_run(source_document_id=sd_id)
        step = await run_state.start_step(run_id=run.id, stage=STAGE_EXTRACT, input_summary={})
        await run_state.complete_step(step.id, output_summary={"total_pages": 10})
        await db_session.commit()

        # Now create the orchestrator with a Stage A mock that should NOT be
        # called (because resume picks up the existing run with a succeeded
        # extract step).
        stage_a = _stage_a_mock()
        orch = PipelineOrchestrator(
            db_session,
            stage_a=stage_a,
            stage_c=_stage_c_mock(candidates_emitted=0),
            stage_d=_stage_d_mock(),
        )
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
            resume=True,
        )

        assert result.run_id == run.id
        # Stage A's run() was NOT called — it was skipped via resume logic.
        stage_a.run.assert_not_awaited()
        assert result.final_status == RUN_SUCCEEDED


# ─── Failure-path step-id capture (greenlet regression) ──────────────────


class TestGreenletCapture:
    """The original greenlet bug: each `_run_*` failure path used `step.id`
    after a commit() that had expired the attribute. After the fix, every
    `_run_*` captures `step_id = step.id` BEFORE the commit. Test by
    forcing each stage to fail and verifying no MissingGreenlet."""

    async def test_stage_a_failure_path_does_not_raise_missing_greenlet(
        self, db_session: AsyncSession
    ) -> None:
        sd_id = await _seed_source_document(db_session)
        stage_a = _stage_a_mock(raise_exc=ValueError("force fail"))

        orch = PipelineOrchestrator(
            db_session, stage_a=stage_a, stage_c=_stage_c_mock(), stage_d=_stage_d_mock()
        )
        # If the greenlet bug regressed, this would raise MissingGreenlet.
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
        )
        # It returned cleanly with the failure recorded.
        assert result.final_status == RUN_FAILED

    async def test_stage_c_failure_path_does_not_raise_missing_greenlet(
        self, db_session: AsyncSession
    ) -> None:
        sd_id = await _seed_source_document(db_session)
        stage_c = _stage_c_mock(raise_exc=RuntimeError("force fail"))

        orch = PipelineOrchestrator(
            db_session, stage_a=_stage_a_mock(), stage_c=stage_c, stage_d=_stage_d_mock()
        )
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
        )
        assert result.final_status == RUN_PARTIALLY_SUCCEEDED

    async def test_stage_d_failure_path_does_not_raise_missing_greenlet(
        self, db_session: AsyncSession
    ) -> None:
        sd_id = await _seed_source_document(db_session)
        run_state = RunStateService(db_session)
        run = await run_state.start_run(source_document_id=sd_id)
        await db_session.commit()
        cand_ids = await _seed_candidates(db_session, run.id, count=1)

        stage_d = _stage_d_mock(raise_for_candidate={cand_ids[0]: RuntimeError("force fail")})
        orch = PipelineOrchestrator(
            db_session,
            stage_a=_stage_a_mock(),
            stage_c=_stage_c_mock(candidates_emitted=1),
            stage_d=stage_d,
        )
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
            resume=True,
        )
        assert result.final_status == RUN_PARTIALLY_SUCCEEDED


# ─── Stage outcome reporting ──────────────────────────────────────────────


class TestStageOutcomeReporting:
    async def test_pipeline_result_stages_includes_each_stage(self, db_session: AsyncSession) -> None:
        sd_id = await _seed_source_document(db_session)

        orch = PipelineOrchestrator(
            db_session,
            stage_a=_stage_a_mock(),
            stage_c=_stage_c_mock(candidates_emitted=0),
            stage_d=_stage_d_mock(),
        )
        result = await orch.run(
            source_document_id=sd_id,
            source_path="/tmp/x.pdf",
            source_type="pdf",
        )

        stages = {s.stage: s.status for s in result.stages}
        assert stages[STAGE_EXTRACT] == "succeeded"
        assert stages[STAGE_MODULE_IDENTIFY] == "succeeded"
        assert stages[STAGE_CARD_DRAFT] == "skipped"
