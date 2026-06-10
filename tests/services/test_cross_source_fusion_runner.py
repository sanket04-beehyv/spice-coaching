"""Tests for CrossSourceFusionRunner — happy path, retire heuristic, draft failure."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import pytest_asyncio
from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.db.models.module import Module
from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.cross_source_fuser import CrossSourceFuser, CrossSourceFuserResult, FusionGroup
from platform_service.services.cross_source_fusion_runner import CrossSourceFusionRunner, FusionRunSummary
from platform_service.services.run_state_service import RUN_SUCCEEDED
from platform_service.workers.stage_d_draft import StageDOrchestrator, StageDResult
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text(
            "TRUNCATE module, module_family, module_candidate_draft, "
            "ingestion_run_step, ingestion_run, source_document "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


async def _seed_source_doc(session: AsyncSession, *, title: str = "doc") -> SourceDocument:
    sd = SourceDocument(
        title=title,
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        assessment_mode="with_quiz",
        authority_label="BRAC",
        original_storage_path="/tmp/x.pdf",
    )
    session.add(sd)
    await session.flush()
    return sd


async def _seed_succeeded_run_with_candidate(
    session: AsyncSession,
    source_document: SourceDocument,
    *,
    proposed_title: str,
    scope_summary: str = "Scope for fusion test.",
) -> tuple[IngestionRun, ModuleCandidateDraft]:
    run = IngestionRun(
        source_document_id=source_document.id,
        status="succeeded",
    )
    session.add(run)
    await session.flush()
    candidate = ModuleCandidateDraft(
        ingestion_run_id=run.id,
        proposed_title=proposed_title,
        scope_summary=scope_summary,
        source_provenance_jsonb=[
            {
                "source_document_id": str(source_document.id),
                "content_block_ids": [],
            }
        ],
        estimated_card_count=5,
        estimated_quiz_count=4,
        proposed_module_type="initial_training",
    )
    session.add(candidate)
    await session.flush()
    return run, candidate


async def _seed_published_module(
    session: AsyncSession,
    *,
    title_en: str,
    source_document_id: UUID,
) -> Module:
    family = ModuleFamily(module_code=f"family-{uuid.uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        title_bn=title_en,
        title_en=title_en,
        description_bn="desc",
        domain="rmnch",
        module_type="initial_training",
        lifecycle_status="published",
        source_document_ids=[source_document_id],
        module_json={"cards": []},
    )
    session.add(module)
    await session.flush()
    family.current_published_module_id = module.id
    await session.flush()
    return module


def _fusion_group(
    *constituents: ModuleCandidateDraft,
    merged_title: str = "Fused ANC Counselling",
) -> FusionGroup:
    return FusionGroup(
        constituent_ids=[c.id for c in constituents],
        merged_title=merged_title,
        merged_scope_summary="Merged scope across sources.",
        pairing_rationale="Same behavioural unit across BRAC + UHIS.",
    )


@dataclass
class _FusionFixture:
    sd_a: SourceDocument
    sd_b: SourceDocument
    cand_a: ModuleCandidateDraft
    cand_b: ModuleCandidateDraft
    doc_ids: list[UUID]


async def _seed_two_source_fusion_inputs(session: AsyncSession) -> _FusionFixture:
    sd_a = await _seed_source_doc(session, title="BRAC clinical")
    sd_b = await _seed_source_doc(session, title="UHIS workflow")
    _, cand_a = await _seed_succeeded_run_with_candidate(session, sd_a, proposed_title="ANC counselling")
    _, cand_b = await _seed_succeeded_run_with_candidate(
        session, sd_b, proposed_title="Conducting ANC visits"
    )
    await session.commit()
    return _FusionFixture(
        sd_a=sd_a,
        sd_b=sd_b,
        cand_a=cand_a,
        cand_b=cand_b,
        doc_ids=[sd_a.id, sd_b.id],
    )


def _mock_fuser(groups: list[FusionGroup]) -> CrossSourceFuser:
    fuser = MagicMock(spec=CrossSourceFuser)
    fuser.fuse = AsyncMock(
        return_value=CrossSourceFuserResult(
            fusion_groups=groups,
            unfused_ids=[],
            raw_response_text="[]",
        )
    )
    return fuser


def _mock_stage_d_success(module_id: UUID | None = None, cards_count: int = 4) -> StageDOrchestrator:
    stage_d = MagicMock(spec=StageDOrchestrator)
    mid = module_id or uuid.uuid4()
    stage_d.run = AsyncMock(
        return_value=StageDResult(
            candidate_id=uuid.uuid4(),
            module_id=mid,
            cards_count=cards_count,
            questions_count=0,
            insufficient_reason=None,
        )
    )
    stage_d._enqueue_post_publish = AsyncMock()
    return stage_d


class TestCrossSourceFusionRunnerHappyPath:
    async def test_run_publishes_fused_module_and_records_summary(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _seed_two_source_fusion_inputs(db_session)
        group = _fusion_group(fx.cand_a, fx.cand_b)
        fuser = _mock_fuser([group])
        stage_d = _mock_stage_d_success()

        runner = CrossSourceFusionRunner(db_session, fuser=fuser, stage_d=stage_d)
        expected_sources = {str(fx.sd_a.id), str(fx.sd_b.id)}
        monkeypatch.setattr(
            runner._draft_orchestrator,
            "_cards_source_set",
            AsyncMock(return_value=expected_sources),
        )
        monkeypatch.setattr(runner._draft_orchestrator, "_enqueue_post_publish", AsyncMock())

        summary = await runner.run(fx.doc_ids)

        assert isinstance(summary, FusionRunSummary)
        assert summary.input_candidate_count == 2
        assert summary.fusion_group_count == 1
        assert summary.fused_modules_published == 1
        assert summary.fused_modules_failed == 0
        assert summary.fused_modules_with_coverage_warning == 0
        assert summary.constituents_retired == 0
        assert len(summary.drafts) == 1
        assert summary.drafts[0]["merged_title"] == group.merged_title
        assert summary.drafts[0]["cross_source_coverage_ok"] is True

        fuser.fuse.assert_awaited_once()
        stage_d.run.assert_awaited()

        run_row = await db_session.get(IngestionRun, summary.fusion_run_id)
        assert run_row is not None
        assert run_row.status == RUN_SUCCEEDED


class TestCrossSourceFusionRunnerRetireHeuristic:
    async def test_retires_constituent_modules_by_title_and_source_overlap(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _seed_two_source_fusion_inputs(db_session)
        mod_a = await _seed_published_module(
            db_session,
            title_en=fx.cand_a.proposed_title,
            source_document_id=fx.sd_a.id,
        )
        mod_b = await _seed_published_module(
            db_session,
            title_en=fx.cand_b.proposed_title,
            source_document_id=fx.sd_b.id,
        )
        await db_session.commit()

        group = _fusion_group(fx.cand_a, fx.cand_b)
        runner = CrossSourceFusionRunner(
            db_session,
            fuser=_mock_fuser([group]),
            stage_d=_mock_stage_d_success(),
        )
        monkeypatch.setattr(
            runner._draft_orchestrator,
            "_cards_source_set",
            AsyncMock(return_value={str(fx.sd_a.id), str(fx.sd_b.id)}),
        )
        monkeypatch.setattr(runner._draft_orchestrator, "_enqueue_post_publish", AsyncMock())

        summary = await runner.run(fx.doc_ids)

        assert summary.constituents_retired == 2
        await db_session.refresh(mod_a)
        await db_session.refresh(mod_b)
        assert mod_a.lifecycle_status == "retired"
        assert mod_b.lifecycle_status == "retired"

    async def test_retire_skips_non_matching_title(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _seed_two_source_fusion_inputs(db_session)
        unrelated = await _seed_published_module(
            db_session,
            title_en="Totally different module",
            source_document_id=fx.sd_a.id,
        )
        await db_session.commit()

        group = _fusion_group(fx.cand_a, fx.cand_b)
        runner = CrossSourceFusionRunner(
            db_session,
            fuser=_mock_fuser([group]),
            stage_d=_mock_stage_d_success(),
        )
        monkeypatch.setattr(
            runner._draft_orchestrator,
            "_cards_source_set",
            AsyncMock(return_value={str(fx.sd_a.id), str(fx.sd_b.id)}),
        )
        monkeypatch.setattr(runner._draft_orchestrator, "_enqueue_post_publish", AsyncMock())

        summary = await runner.run(fx.doc_ids)

        assert summary.constituents_retired == 0
        await db_session.refresh(unrelated)
        assert unrelated.lifecycle_status == "published"


class TestCrossSourceFusionRunnerDraftFailure:
    async def test_draft_failure_increments_failed_and_skips_post_publish(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _seed_two_source_fusion_inputs(db_session)
        group = _fusion_group(fx.cand_a, fx.cand_b)
        stage_d = MagicMock(spec=StageDOrchestrator)
        stage_d.run = AsyncMock(side_effect=RuntimeError("Vertex truncated JSON"))
        stage_d._enqueue_post_publish = AsyncMock()

        runner = CrossSourceFusionRunner(
            db_session,
            fuser=_mock_fuser([group]),
            stage_d=stage_d,
        )
        enqueue_mock = AsyncMock()
        monkeypatch.setattr(runner._draft_orchestrator, "_enqueue_post_publish", enqueue_mock)

        summary = await runner.run(fx.doc_ids)

        assert summary.fused_modules_published == 0
        assert summary.fused_modules_failed == 1
        assert summary.drafts[0]["module_id"] is None
        assert summary.drafts[0]["insufficient_reason"] == "RuntimeError"
        enqueue_mock.assert_not_awaited()

    async def test_coverage_warning_still_publishes_module(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = await _seed_two_source_fusion_inputs(db_session)
        group = _fusion_group(fx.cand_a, fx.cand_b)
        module_id = uuid.uuid4()
        stage_d = _mock_stage_d_success(module_id=module_id)

        runner = CrossSourceFusionRunner(
            db_session,
            fuser=_mock_fuser([group]),
            stage_d=stage_d,
        )
        # Both coverage attempts see only one source — accept best-effort on attempt 2.
        monkeypatch.setattr(
            runner._draft_orchestrator,
            "_cards_source_set",
            AsyncMock(return_value={str(fx.sd_a.id)}),
        )
        enqueue_mock = AsyncMock()
        monkeypatch.setattr(runner._draft_orchestrator, "_enqueue_post_publish", enqueue_mock)

        summary = await runner.run(fx.doc_ids)

        assert summary.fused_modules_published == 1
        assert summary.fused_modules_with_coverage_warning == 1
        assert summary.drafts[0]["cross_source_coverage_ok"] is False
        enqueue_mock.assert_awaited_once()
