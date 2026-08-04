"""Unit tests for ingest failed-stage retry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from mc_foundation.problem import AppError
from platform_service.config import get_settings
from platform_service.db.models.ingest_batch import IngestBatch
from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.services.ingest_retry_service import IngestRetryService
from platform_service.services.run_state_service import (
    BATCH_FAILED,
    RUN_FAILED,
    RUN_PARTIALLY_SUCCEEDED,
    RUN_RUNNING,
    STAGE_CARD_DRAFT,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    STAGE_QUIZ_GENERATION,
    STEP_FAILED,
    STEP_SUCCEEDED,
    RunStateService,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(
        db_session,
        "attribution_event, module_candidate_draft, source_page, ingestion_run_step, ingestion_run, ingest_batch, source_document",
    )
    yield


async def _seed_batch_run(
    session: AsyncSession,
    *,
    run_status: str = RUN_FAILED,
) -> tuple[IngestBatch, SourceDocument, IngestionRun]:
    batch = IngestBatch(status=BATCH_FAILED)
    session.add(batch)
    await session.flush()
    doc = SourceDocument(
        title="retry-doc",
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        original_storage_path="bucket/ingest/retry.pdf",
        status="failed",
    )
    session.add(doc)
    await session.flush()
    run = IngestionRun(
        source_document_id=doc.id,
        ingest_batch_id=batch.id,
        status=run_status,
        error_jsonb={"failed_stage": STAGE_EXTRACT},
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    await session.commit()
    return batch, doc, run


class TestFindStepLatestMatch:
    async def test_find_step_returns_latest_matching(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session, run_status=RUN_RUNNING)
        del batch
        cand = str(uuid4())
        older = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_CARD_DRAFT,
            status=STEP_FAILED,
            started_at=datetime.now(UTC) - timedelta(minutes=5),
            input_summary_jsonb={"candidate_id": cand},
        )
        newer = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_CARD_DRAFT,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"candidate_id": cand},
        )
        db_session.add_all([older, newer])
        await db_session.commit()

        run_state = RunStateService(db_session)
        step = await run_state.find_step(run.id, stage=STAGE_CARD_DRAFT, input_match={"candidate_id": cand})
        assert step is not None
        assert step.id == newer.id
        assert await run_state.is_stage_succeeded(
            run.id, stage=STAGE_CARD_DRAFT, input_match={"candidate_id": cand}
        )


class TestIngestRetryService:
    async def test_noop_when_step_not_failed(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session)
        step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_EXTRACT,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
        )
        db_session.add(step)
        await db_session.commit()

        result = await IngestRetryService(db_session).retry(
            batch_id=batch.id,
            run_id=run.id,
            stage=STAGE_EXTRACT,
        )
        assert result.status == "noop"
        assert result.reason == "step_not_failed"

    async def test_noop_when_already_running_with_claim(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session, run_status=RUN_RUNNING)
        run.error_jsonb = {
            "_pipeline_claim": {
                "claim_token": "worker-a",
                "claimed_at": datetime.now(UTC).isoformat(),
            }
        }
        step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_EXTRACT,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
        )
        db_session.add(step)
        await db_session.commit()

        result = await IngestRetryService(db_session).retry(
            batch_id=batch.id,
            run_id=run.id,
            stage=STAGE_EXTRACT,
        )
        assert result.status == "noop"
        assert result.reason == "already_running"

    async def test_extract_retry_clears_downstream_and_enqueues(self, db_session: AsyncSession) -> None:
        batch, doc, run = await _seed_batch_run(db_session)
        page = SourcePage(
            source_document_id=doc.id,
            page_number=1,
            markdown_content="# Hello",
            extraction_method="text",
            extraction_quality_score=1.0,
        )
        db_session.add(page)
        cand = ModuleCandidateDraft(
            ingestion_run_id=run.id,
            proposed_title="Cand",
            scope_summary="s",
            source_provenance_jsonb=[],
        )
        db_session.add(cand)
        await db_session.flush()
        extract_step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_EXTRACT,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
            error_jsonb={"type": "Boom", "message": "fail"},
        )
        identify_step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
        )
        draft_step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_CARD_DRAFT,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"candidate_id": str(cand.id)},
        )
        db_session.add_all([extract_step, identify_step, draft_step])
        await db_session.commit()

        with patch("platform_service.services.ingest_retry_service.enqueue_pipeline_resume") as enqueue:
            result = await IngestRetryService(db_session).retry(
                batch_id=batch.id,
                run_id=run.id,
                stage=STAGE_EXTRACT,
            )

        assert result.status == "retry_queued"
        enqueue.assert_called_once()
        await db_session.refresh(run)
        assert run.status == RUN_RUNNING
        assert run.completed_at is None
        steps = (
            (
                await db_session.execute(
                    select(IngestionRunStep).where(IngestionRunStep.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert steps == []
        pages = (
            (await db_session.execute(select(SourcePage).where(SourcePage.source_document_id == doc.id)))
            .scalars()
            .all()
        )
        assert pages == []
        cands = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert cands == []

    async def test_card_draft_retry_only_touches_failed_candidate(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session, run_status=RUN_PARTIALLY_SUCCEEDED)
        ok_cand = ModuleCandidateDraft(
            ingestion_run_id=run.id,
            proposed_title="OK",
            scope_summary="s",
            source_provenance_jsonb=[],
        )
        bad_cand = ModuleCandidateDraft(
            ingestion_run_id=run.id,
            proposed_title="Bad",
            scope_summary="s",
            source_provenance_jsonb=[],
        )
        db_session.add_all([ok_cand, bad_cand])
        await db_session.flush()
        ok_step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_CARD_DRAFT,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"candidate_id": str(ok_cand.id)},
            output_summary_jsonb={"module_id": str(uuid4())},
        )
        bad_step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_CARD_DRAFT,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"candidate_id": str(bad_cand.id)},
        )
        quiz_step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_QUIZ_GENERATION,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={
                "candidate_id": str(bad_cand.id),
                "module_id": str(uuid4()),
            },
        )
        db_session.add_all([ok_step, bad_step, quiz_step])
        await db_session.commit()

        with patch("platform_service.services.ingest_retry_service.enqueue_pipeline_resume") as enqueue:
            result = await IngestRetryService(db_session).retry(
                batch_id=batch.id,
                run_id=run.id,
                stage=STAGE_CARD_DRAFT,
                candidate_id=bad_cand.id,
            )

        assert result.status == "retry_queued"
        enqueue.assert_called_once()
        remaining = (
            (
                await db_session.execute(
                    select(IngestionRunStep).where(IngestionRunStep.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining) == 1
        assert remaining[0].id == ok_step.id
        cands = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert {c.id for c in cands} == {ok_cand.id, bad_cand.id}

    async def test_post_publish_retry_reenqueues_same_step(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session, run_status=RUN_PARTIALLY_SUCCEEDED)
        module_id = uuid4()
        cand_id = uuid4()
        step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_QUIZ_GENERATION,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={
                "candidate_id": str(cand_id),
                "module_id": str(module_id),
            },
            error_jsonb={"type": "QuizError", "message": "fail"},
        )
        db_session.add(step)
        await db_session.commit()

        with patch(
            "platform_service.services.ingest_retry_service.enqueue_post_publish_step_retry"
        ) as enqueue:
            result = await IngestRetryService(db_session).retry(
                batch_id=batch.id,
                run_id=run.id,
                stage=STAGE_QUIZ_GENERATION,
                candidate_id=cand_id,
            )

        assert result.status == "retry_queued"
        enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        assert kwargs["module_id"] == module_id
        assert kwargs["step_id"] == step.id
        await db_session.refresh(step)
        assert step.status == "running"
        assert step.error_jsonb is None

    async def test_missing_candidate_id_for_card_draft(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session)
        with pytest.raises(AppError) as exc:
            await IngestRetryService(db_session).retry(
                batch_id=batch.id,
                run_id=run.id,
                stage=STAGE_CARD_DRAFT,
            )
        assert exc.value.code == "candidate_required"

    async def test_run_not_in_batch(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session)
        other = IngestBatch(status=BATCH_FAILED)
        db_session.add(other)
        await db_session.commit()
        with pytest.raises(AppError) as exc:
            await IngestRetryService(db_session).retry(
                batch_id=other.id,
                run_id=run.id,
                stage=STAGE_EXTRACT,
            )
        assert exc.value.code == "run_not_found"

    async def test_chunk_identify_retry_resets_chunk_keeps_sibling(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session, run_status=RUN_PARTIALLY_SUCCEEDED)
        keep = ModuleCandidateDraft(
            ingestion_run_id=run.id,
            proposed_title="Keep",
            scope_summary="s",
            source_provenance_jsonb=[],
            source_chunk_ids=["chunk-1"],
        )
        drop = ModuleCandidateDraft(
            ingestion_run_id=run.id,
            proposed_title="Drop",
            scope_summary="s",
            source_provenance_jsonb=[],
            source_chunk_ids=["chunk-2"],
        )
        db_session.add_all([keep, drop])
        await db_session.flush()
        parent = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
            output_summary_jsonb={"chunks_failed": 1},
        )
        ok_chunk = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"chunk_id": "chunk-1"},
        )
        bad_chunk = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"chunk_id": "chunk-2"},
            error_jsonb={"type": "Timeout", "message": "fail"},
        )
        keep_draft = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_CARD_DRAFT,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"candidate_id": str(keep.id)},
        )
        db_session.add_all([parent, ok_chunk, bad_chunk, keep_draft])
        await db_session.commit()

        with patch("platform_service.services.ingest_retry_service.enqueue_pipeline_resume") as enqueue:
            result = await IngestRetryService(db_session).retry(
                batch_id=batch.id,
                run_id=run.id,
                stage=STAGE_MODULE_IDENTIFY,
                chunk_id="chunk-2",
            )

        assert result.status == "retry_queued"
        assert result.chunk_id == "chunk-2"
        enqueue.assert_called_once()
        assert enqueue.call_args.kwargs["identify_chunk_ids"] == ["chunk-2"]
        await db_session.refresh(bad_chunk)
        assert bad_chunk.status == "running"
        assert bad_chunk.error_jsonb is None
        cands = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert {c.id for c in cands} == {keep.id}
        steps = (
            (
                await db_session.execute(
                    select(IngestionRunStep).where(IngestionRunStep.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(s.id == keep_draft.id for s in steps)
        assert any(s.id == ok_chunk.id for s in steps)

    async def test_chunk_retry_noop_when_not_failed(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session)
        step = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"chunk_id": "chunk-1"},
        )
        db_session.add(step)
        await db_session.commit()
        result = await IngestRetryService(db_session).retry(
            batch_id=batch.id,
            run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            chunk_id="chunk-1",
        )
        assert result.status == "noop"
        assert result.reason == "step_not_failed"

    async def test_whole_stage_identify_uses_parent_not_chunk(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session)
        parent = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_FAILED,
            started_at=datetime.now(UTC) - timedelta(minutes=1),
            error_jsonb={"type": "AllChunksFailed", "message": "all failed"},
        )
        chunk = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"chunk_id": "chunk-1"},
        )
        cand = ModuleCandidateDraft(
            ingestion_run_id=run.id,
            proposed_title="X",
            scope_summary="s",
            source_provenance_jsonb=[],
            source_chunk_ids=["chunk-1"],
        )
        db_session.add_all([parent, chunk, cand])
        await db_session.commit()

        with patch("platform_service.services.ingest_retry_service.enqueue_pipeline_resume") as enqueue:
            result = await IngestRetryService(db_session).retry(
                batch_id=batch.id,
                run_id=run.id,
                stage=STAGE_MODULE_IDENTIFY,
            )

        assert result.status == "retry_queued"
        assert result.chunk_id is None
        enqueue.assert_called_once()
        assert "identify_chunk_ids" not in enqueue.call_args.kwargs
        cands = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert cands == []
        steps = (
            (
                await db_session.execute(
                    select(IngestionRunStep).where(IngestionRunStep.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert steps == []


class TestIngestRetryBatch:
    async def test_retry_batch_retries_all_failed_stages(self, db_session: AsyncSession) -> None:
        batch, _doc, run = await _seed_batch_run(db_session)
        db_session.add(
            IngestionRunStep(
                ingestion_run_id=run.id,
                stage=STAGE_EXTRACT,
                status=STEP_FAILED,
                started_at=datetime.now(UTC),
                error_jsonb={"type": "Boom", "message": "fail"},
            )
        )
        db_session.add(
            IngestionRunStep(
                ingestion_run_id=run.id,
                stage=STAGE_MODULE_IDENTIFY,
                status=STEP_FAILED,
                started_at=datetime.now(UTC),
                input_summary_jsonb={"chunk_id": "chunk-1"},
            )
        )
        await db_session.commit()

        with patch("platform_service.services.ingest_retry_service.enqueue_pipeline_resume") as enqueue:
            result = await IngestRetryService(db_session).retry_batch(batch.id)

        assert result.batch_id == batch.id
        assert result.poll_url == get_settings().api_path(f"/admin/ingest/batches/{batch.id}")
        assert len(result.results) == 2
        assert result.results[0].stage == STAGE_EXTRACT
        assert result.results[0].status == "retry_queued"
        assert result.results[1].stage == STAGE_MODULE_IDENTIFY
        assert result.results[1].chunk_id == "chunk-1"
        assert result.results[1].status in {"retry_queued", "noop"}
        assert enqueue.call_count >= 1

    async def test_retry_batch_unknown_batch_raises(self, db_session: AsyncSession) -> None:
        with pytest.raises(AppError) as exc_info:
            await IngestRetryService(db_session).retry_batch(uuid4())
        assert exc_info.value.code == "batch_not_found"


class TestReopenRunForRetry:
    async def test_reopen_clears_terminal_error_and_claim(self, db_session: AsyncSession) -> None:
        _batch, _doc, run = await _seed_batch_run(db_session)
        run.error_jsonb = {
            "failed_stage": STAGE_EXTRACT,
            "detail": "pipeline crashed",
            "_pipeline_claim": {
                "claim_token": "old",
                "claimed_at": datetime.now(UTC).isoformat(),
            },
        }
        await db_session.commit()
        run_state = RunStateService(db_session)
        reopened = await run_state.reopen_run_for_retry(run.id)
        await db_session.commit()
        assert reopened.status == RUN_RUNNING
        assert reopened.completed_at is None
        assert reopened.error_jsonb is None or "_pipeline_claim" not in (reopened.error_jsonb or {})
        assert "failed_stage" not in (reopened.error_jsonb or {})
