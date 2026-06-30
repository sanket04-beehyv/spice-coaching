"""Tests for post-publish step creation on Stage D enqueue."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from platform_service.config import get_settings
from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.run_state_service import (
    RUN_RUNNING,
    STAGE_CARD_SEARCH_METADATA_GENERATION,
    STAGE_EMBEDDING_GENERATION,
    STAGE_GAP_CLASSIFICATION,
    STAGE_QUIZ_GENERATION,
    STAGE_SEARCH_METADATA_GENERATION,
    STEP_SKIPPED,
    RunStateService,
)
from platform_service.workers.stage_d_draft import StageDOrchestrator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text(
            "TRUNCATE module_candidate_draft, source_document, ingestion_run_step, ingestion_run RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def _seed_run_and_candidate(
    session: AsyncSession,
    *,
    assessment_mode: str = "with_quiz",
) -> tuple[StageDOrchestrator, IngestionRun, ModuleCandidateDraft, SourceDocument]:
    sd = SourceDocument(
        title="t",
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        assessment_mode=assessment_mode,
        authority_label="BRAC",
        original_storage_path="/tmp/x.pdf",
    )
    session.add(sd)
    await session.flush()
    run_state = RunStateService(session)
    run = await run_state.start_run(source_document_id=sd.id)
    cand = ModuleCandidateDraft(
        ingestion_run_id=run.id,
        proposed_title="T",
        scope_summary="x",
        source_provenance_jsonb=[{"source_document_id": str(sd.id), "content_block_ids": []}],
        estimated_card_count=5,
        estimated_quiz_count=4,
        proposed_module_type="refresher",
    )
    session.add(cand)
    await session.flush()
    return StageDOrchestrator(session), run, cand, sd


class TestEnqueuePostPublishSteps:
    async def test_creates_quiz_and_embedding_steps(self, db_session: AsyncSession) -> None:
        stage_d, run, cand, sd = await _seed_run_and_candidate(db_session)
        module_id = uuid4()
        mock_quiz = MagicMock()
        mock_embed = MagicMock()
        mock_gap = MagicMock()
        mock_metadata = MagicMock()
        mock_card_batch = MagicMock()

        with (
            patch("platform_service.celery_tasks.generate_module_quiz_task", mock_quiz),
            patch("platform_service.celery_tasks.generate_module_embedding_task", mock_embed),
            patch("platform_service.celery_tasks.generate_module_search_metadata_task", mock_metadata),
            patch(
                "platform_service.celery_tasks.generate_module_card_search_metadata_batch_task",
                mock_card_batch,
            ),
            patch("platform_service.celery_tasks.classify_module_gaps_task", mock_gap),
        ):
            await stage_d._enqueue_post_publish(
                module_id,
                [sd.id],
                ingestion_run_id=run.id,
                candidate_id=cand.id,
            )

        steps = list(
            (
                await db_session.execute(
                    select(IngestionRunStep).where(IngestionRunStep.ingestion_run_id == run.id)
                )
            ).scalars()
        )
        stages = {s.stage: s for s in steps}
        assert STAGE_QUIZ_GENERATION in stages
        assert STAGE_EMBEDDING_GENERATION in stages
        assert STAGE_SEARCH_METADATA_GENERATION in stages
        assert STAGE_CARD_SEARCH_METADATA_GENERATION in stages
        assert STAGE_GAP_CLASSIFICATION in stages
        assert stages[STAGE_QUIZ_GENERATION].status == "running"
        mock_quiz.delay.assert_called_once()
        mock_card_batch.delay.assert_called_once()
        mock_metadata.delay.assert_not_called()
        mock_embed.delay.assert_not_called()
        mock_gap.delay.assert_called_once()

        run_row = await db_session.get(IngestionRun, run.id)
        assert run_row is not None
        assert run_row.status == RUN_RUNNING

    async def test_read_only_skips_quiz_step(self, db_session: AsyncSession) -> None:
        stage_d, run, cand, sd = await _seed_run_and_candidate(db_session, assessment_mode="read_only")
        module_id = uuid4()
        mock_quiz = MagicMock()
        mock_embed = MagicMock()
        mock_gap = MagicMock()
        mock_metadata = MagicMock()
        mock_card_batch = MagicMock()

        with (
            patch("platform_service.celery_tasks.generate_module_quiz_task", mock_quiz),
            patch("platform_service.celery_tasks.generate_module_embedding_task", mock_embed),
            patch("platform_service.celery_tasks.generate_module_search_metadata_task", mock_metadata),
            patch(
                "platform_service.celery_tasks.generate_module_card_search_metadata_batch_task",
                mock_card_batch,
            ),
            patch("platform_service.celery_tasks.classify_module_gaps_task", mock_gap),
        ):
            await stage_d._enqueue_post_publish(
                module_id,
                [sd.id],
                ingestion_run_id=run.id,
                candidate_id=cand.id,
            )

        steps = list(
            (
                await db_session.execute(
                    select(IngestionRunStep).where(IngestionRunStep.ingestion_run_id == run.id)
                )
            ).scalars()
        )
        quiz_steps = [s for s in steps if s.stage == STAGE_QUIZ_GENERATION]
        assert len(quiz_steps) == 1
        assert quiz_steps[0].status == STEP_SKIPPED
        mock_quiz.delay.assert_not_called()
        mock_card_batch.delay.assert_called_once()
        mock_metadata.delay.assert_not_called()
        mock_embed.delay.assert_not_called()
        mock_gap.delay.assert_called_once()

    async def test_metadata_disabled_enqueues_embedding_directly(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stage_d, run, cand, sd = await _seed_run_and_candidate(db_session)
        module_id = uuid4()
        mock_quiz = MagicMock()
        mock_embed = MagicMock()
        mock_gap = MagicMock()
        mock_metadata = MagicMock()
        mock_card_batch = MagicMock()

        settings = get_settings()
        monkeypatch.setattr(settings, "post_publish_search_metadata_enabled", False)

        with (
            patch("platform_service.celery_tasks.generate_module_quiz_task", mock_quiz),
            patch("platform_service.celery_tasks.generate_module_embedding_task", mock_embed),
            patch("platform_service.celery_tasks.generate_module_search_metadata_task", mock_metadata),
            patch(
                "platform_service.celery_tasks.generate_module_card_search_metadata_batch_task",
                mock_card_batch,
            ),
            patch("platform_service.celery_tasks.classify_module_gaps_task", mock_gap),
            patch("platform_service.services.draft_pipeline.get_settings", lambda: settings),
        ):
            await stage_d._enqueue_post_publish(
                module_id,
                [sd.id],
                ingestion_run_id=run.id,
                candidate_id=cand.id,
            )

        mock_metadata.delay.assert_not_called()
        mock_card_batch.delay.assert_not_called()
        mock_embed.delay.assert_called_once()
