"""Layer 2 chunk 5 — Stage 2 (Stage C) identify orchestrator tests.

Critical regressions covered:

- **Architecture-reset P1**: every LLM-emitted candidate is persisted, even
  ones the insufficient-source filter would have flagged. Quality flags
  land on `module_candidate_draft.quality_flags_jsonb`; pipeline never
  rejects.
- **Architecture-reset gap removal**: Stage C does NOT load the
  `behavioural_gap` table or pass `valid_gap_codes` to the identifier.
- Empty workspace returns 0 candidates without calling the identifier.
- `_ensure_content_blocks` parses `markdown_content` into `content_block`
  rows on first access, then is idempotent on subsequent runs.
- `behavioural_gap_code` on persisted candidates is None (gap context
  removed from prompt).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.content_block import ContentBlock
from platform_service.db.models.ingest_batch import IngestBatch
from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.services.module_identifier import (
    ModuleIdentifier,
    ModuleIdentifierError,
    ModuleIdentifierResult,
)
from platform_service.services.run_state_service import (
    STAGE_MODULE_IDENTIFY,
    STEP_FAILED,
)
from platform_service.workers.stage_c_identify import StageCOrchestrator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]

# ─── Cleanup ──────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(
        db_session,
        "module_candidate_draft, content_block, source_page, source_document, ingestion_run_step, ingestion_run, ingest_batch, behavioural_gap, module, module_family",
    )
    yield


async def _seed_source_doc_with_outline(
    session: AsyncSession,
    *,
    page_count: int = 2,
    sections: list[dict] | None = None,
    block_text: str = "Body content of the page.",
) -> tuple[SourceDocument, list[ContentBlock]]:
    """Create a source_document with N pages, each with one content_block.
    Returns (doc, blocks). Used for happy-path tests."""
    sd = SourceDocument(
        title="seed",
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        original_storage_path="/tmp/x.pdf",
        outline_method="markdown_parser",
        outline_jsonb={"sections": sections or [{"heading": "Sec 1"}]},
    )
    session.add(sd)
    await session.flush()
    blocks: list[ContentBlock] = []
    for page_no in range(1, page_count + 1):
        sp = SourcePage(
            source_document_id=sd.id,
            page_number=page_no,
            markdown_content=f"# Page {page_no}\n\n{block_text}",
            extraction_method="text",
            extraction_quality_score=0.9,
        )
        session.add(sp)
        await session.flush()
        cb = ContentBlock(
            source_page_id=sp.id,
            block_order=0,
            block_type="text",
            content_text=block_text,
            heading_path_jsonb=["Sec 1"],
        )
        session.add(cb)
        await session.flush()
        blocks.append(cb)
    await session.commit()
    return sd, blocks


async def _seed_run(
    session: AsyncSession,
    source_document_id: UUID,
    *,
    ingestion_instructions: str | None = None,
    assessment_mode: str = "with_quiz",
    cards_per_module: int | None = None,
    quizzes_per_module: int | None = None,
) -> IngestionRun:
    batch = IngestBatch(
        status="running",
        assessment_mode=assessment_mode,
        ingestion_instructions=ingestion_instructions,
        cards_per_module=cards_per_module,
        quizzes_per_module=quizzes_per_module,
    )
    session.add(batch)
    await session.flush()
    run = IngestionRun(
        source_document_id=source_document_id,
        ingest_batch_id=batch.id,
        status="running",
    )
    session.add(run)
    await session.flush()
    await session.commit()
    return run


def _llm_candidate(
    block_ids: list[UUID],
    *,
    title: str = "T",
    cards: int = 5,
    quiz: int = 4,
    domain: str | None = None,
) -> dict:
    out = {
        "proposed_title": title,
        "scope_summary": "Summary of the topic.",
        "source_provenance": [
            {
                "source_document_id": str(uuid4()),  # ignored by repo
                "source_page_id": str(uuid4()),
                "content_block_ids": [str(b) for b in block_ids],
            }
        ],
        "estimated_card_count": cards,
        "estimated_quiz_count": quiz,
        "proposed_module_type": "refresher",
    }
    if domain is not None:
        out["domain"] = domain
    return out


def _identifier_mock(candidates: list[dict]) -> MagicMock:
    ident = MagicMock(spec=ModuleIdentifier)
    ident.identify = AsyncMock(
        return_value=ModuleIdentifierResult(candidates=candidates, raw_response_text="")
    )
    return ident


def _identifier_mock_per_call(per_call_candidates: list[list[dict]]) -> MagicMock:
    """Mock that returns a different candidate list on each call (in order).

    Used when a test partitions a doc into N sections and wants each
    per-section call to produce distinct output. With `_identifier_mock`,
    every call returns the same fixture and the test can't distinguish
    section behaviours.
    """
    ident = MagicMock(spec=ModuleIdentifier)
    results = [
        ModuleIdentifierResult(candidates=cands, raw_response_text="") for cands in per_call_candidates
    ]
    ident.identify = AsyncMock(side_effect=results)
    return ident


# ─── Empty workspace ───────────────────────────────────────────────────────


class TestEmptyWorkspace:
    async def test_no_documents_returns_zero_candidates(self, db_session: AsyncSession) -> None:
        ident = MagicMock()
        ident.identify = AsyncMock()  # spy
        orch = StageCOrchestrator(db_session, identifier=ident)

        result = await orch.run(ingestion_run_id=uuid4(), source_document_ids=[uuid4()])

        assert result.candidates_emitted == 0
        assert result.candidates_flagged == 0
        # Identifier never called when there are no docs.
        ident.identify.assert_not_awaited()


# ─── Architecture-reset P1: all candidates emitted, flags written ─────────


class TestQualityFlagsAreAdvisory:
    """Per the P1 architecture-reset fix, the insufficient-source filter no
    longer rejects candidates. Every LLM-emitted candidate must be persisted
    as a module_candidate_draft row, with `quality_flags_jsonb` populated
    when the heuristic flagged it."""

    async def test_thin_provenance_candidate_still_emitted_with_flags(self, db_session: AsyncSession) -> None:
        sd, blocks = await _seed_source_doc_with_outline(
            db_session,
            page_count=1,
            block_text="x",  # very short → token threshold flag
        )
        run = await _seed_run(db_session, sd.id)

        ident = _identifier_mock([_llm_candidate([blocks[0].id], title="thin")])
        orch = StageCOrchestrator(db_session, identifier=ident)
        result = await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        # Emitted (not rejected) but counted as flagged.
        assert result.candidates_emitted == 1
        assert result.candidates_flagged == 1

        # Row has quality_flags_jsonb populated with the flag.
        rows = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        flags = rows[0].quality_flags_jsonb or {}
        assert "insufficient_tokens" in (flags.get("flags") or [])

    async def test_well_formed_candidate_has_no_quality_flags(self, db_session: AsyncSession) -> None:
        # Block content beefed up to clear the token threshold.
        sd, blocks = await _seed_source_doc_with_outline(
            db_session,
            page_count=1,
            block_text=" ".join(f"word{i}" for i in range(200)),  # 200 tokens
            sections=[{"heading": "Sec A"}],
        )
        run = await _seed_run(db_session, sd.id)
        ident = _identifier_mock([_llm_candidate([blocks[0].id], title="solid")])

        orch = StageCOrchestrator(db_session, identifier=ident)
        result = await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        assert result.candidates_emitted == 1
        rows = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        # No flags → quality_flags_jsonb is NULL (vs {"flags": []}).
        assert rows[0].quality_flags_jsonb is None


# ─── Ingestion instructions ────────────────────────────────────────────────


class TestIngestionInstructionsPassedToIdentifier:
    async def test_passes_instructions_from_ingest_batch(self, db_session: AsyncSession) -> None:
        sd, blocks = await _seed_source_doc_with_outline(db_session, page_count=1)
        run = await _seed_run(
            db_session,
            sd.id,
            ingestion_instructions="Prioritize ANC referral modules.",
        )
        ident = _identifier_mock([_llm_candidate([blocks[0].id])])
        orch = StageCOrchestrator(db_session, identifier=ident)
        result = await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        assert result.candidates_emitted == 1
        assert result.ingestion_instructions_present is True
        ident.identify.assert_awaited()
        call_kwargs = ident.identify.await_args.kwargs
        assert call_kwargs["ingestion_instructions"] == "Prioritize ANC referral modules."

    async def test_persists_ingestion_instruction_rationale(self, db_session: AsyncSession) -> None:
        sd, blocks = await _seed_source_doc_with_outline(db_session, page_count=1)
        run = await _seed_run(
            db_session,
            sd.id,
            ingestion_instructions="Prioritize ANC referral modules.",
        )
        cand = _llm_candidate([blocks[0].id])
        cand["ingestion_instruction_rationale"] = "Chapter 2 covers ANC referral workflows."
        ident = _identifier_mock([cand])
        orch = StageCOrchestrator(db_session, identifier=ident)
        await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        result = await db_session.execute(
            select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
        )
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].ingestion_instruction_rationale == "Chapter 2 covers ANC referral workflows."

    async def test_no_instructions_when_absent(self, db_session: AsyncSession) -> None:
        sd, blocks = await _seed_source_doc_with_outline(db_session, page_count=1)
        run = await _seed_run(db_session, sd.id)
        ident = _identifier_mock([_llm_candidate([blocks[0].id])])
        orch = StageCOrchestrator(db_session, identifier=ident)
        result = await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        assert result.ingestion_instructions_present is False
        call_kwargs = ident.identify.await_args.kwargs
        assert call_kwargs["ingestion_instructions"] is None


# ─── Architecture-reset: gap context not loaded ────────────────────────────


class TestNoGapContextLoaded:
    async def test_does_not_query_behavioural_gap_table(self, db_session: AsyncSession) -> None:
        # Pre-seed a gap row so we'd notice if the orchestrator queried it.
        gap = BehaviouralGap(
            gap_code="test:should_not_be_loaded",
            description="If Stage C loads this, the test fails — it should not.",
            domain="rmnch",
            severity_default="medium",
        )
        db_session.add(gap)
        await db_session.commit()

        sd, blocks = await _seed_source_doc_with_outline(db_session)
        run = await _seed_run(db_session, sd.id)

        ident = _identifier_mock([_llm_candidate([blocks[0].id])])
        orch = StageCOrchestrator(db_session, identifier=ident)
        await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        # The identifier was called WITHOUT `valid_gap_codes` or
        # `behavioural_gap_registry` kwargs (architecture-reset removed them).
        kwargs = ident.identify.call_args.kwargs
        assert "valid_gap_codes" not in kwargs
        assert "behavioural_gap_registry" not in kwargs

    async def test_persisted_candidate_has_null_gap_code(self, db_session: AsyncSession) -> None:
        sd, blocks = await _seed_source_doc_with_outline(db_session)
        run = await _seed_run(db_session, sd.id)
        ident = _identifier_mock([_llm_candidate([blocks[0].id])])

        orch = StageCOrchestrator(db_session, identifier=ident)
        await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        rows = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        # Architecture-reset: behavioural_gap_code is NULL on every emitted
        # candidate (Stage 2 prompt no longer reasons about gaps).
        assert rows[0].behavioural_gap_code is None

    async def test_persisted_candidate_carries_domain_from_stage_c(self, db_session: AsyncSession) -> None:
        sd, blocks = await _seed_source_doc_with_outline(db_session)
        run = await _seed_run(db_session, sd.id)
        ident = _identifier_mock([_llm_candidate([blocks[0].id], domain="family_planning")])

        orch = StageCOrchestrator(db_session, identifier=ident)
        await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        rows = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].domain == "family_planning"


# ─── Identifier failure path ──────────────────────────────────────────────


class TestIdentifierFailures:
    async def test_chunk_failure_does_not_raise(self, db_session: AsyncSession) -> None:
        """In the chunked architecture, a per-chunk identifier failure is
        logged and counted but does NOT abort Stage 2. The run completes
        with chunks_failed > 0 and candidates_emitted reflecting only the
        chunks that succeeded. Each failed chunk gets a failed run step."""
        sd, _ = await _seed_source_doc_with_outline(db_session)
        run = await _seed_run(db_session, sd.id)

        ident = MagicMock(spec=ModuleIdentifier)
        ident.identify = AsyncMock(side_effect=ModuleIdentifierError("Vertex 503"))

        orch = StageCOrchestrator(db_session, identifier=ident)
        result = await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        assert result.candidates_emitted == 0
        assert result.chunks_failed >= 1
        assert result.chunks_succeeded == 0

        chunk_steps = (
            (
                await db_session.execute(
                    select(IngestionRunStep).where(
                        IngestionRunStep.ingestion_run_id == run.id,
                        IngestionRunStep.stage == STAGE_MODULE_IDENTIFY,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert chunk_steps
        assert all(s.status == STEP_FAILED for s in chunk_steps)
        assert all((s.input_summary_jsonb or {}).get("chunk_id") for s in chunk_steps)
        assert all(s.error_jsonb and s.error_jsonb.get("type") for s in chunk_steps)

    async def test_persists_source_chunk_ids_on_success(self, db_session: AsyncSession) -> None:
        sd, blocks = await _seed_source_doc_with_outline(db_session)
        run = await _seed_run(db_session, sd.id)
        ident = _identifier_mock([_llm_candidate([blocks[0].id], title="Chunked")])
        orch = StageCOrchestrator(db_session, identifier=ident)
        result = await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])
        assert result.candidates_emitted == 1
        rows = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].source_chunk_ids
        assert all(isinstance(x, str) and x.startswith("chunk-") for x in rows[0].source_chunk_ids)


# ─── Output summary shape ─────────────────────────────────────────────────


class TestOutputSummary:
    async def test_emitted_count_matches_persisted_rows(self, db_session: AsyncSession) -> None:
        sd, blocks = await _seed_source_doc_with_outline(db_session)
        run = await _seed_run(db_session, sd.id)

        # Three good candidates.
        ident = _identifier_mock(
            [
                _llm_candidate([blocks[0].id], title="A"),
                _llm_candidate([blocks[0].id], title="B"),
                _llm_candidate([blocks[0].id], title="C"),
            ]
        )

        orch = StageCOrchestrator(db_session, identifier=ident)
        result = await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        assert result.candidates_emitted == 3
        # P1: candidates are never rejected — flagged ones are emitted too.
        rows = (
            (
                await db_session.execute(
                    select(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 3

    async def test_flag_counts_aggregate_across_candidates(self, db_session: AsyncSession) -> None:
        """Per-flag counts are surfaced under `flag_counts` for the
        dashboard's run-summary card. Flagged candidates are still
        emitted; this test verifies the aggregate counts."""
        sd, blocks = await _seed_source_doc_with_outline(
            db_session,
            block_text="x",  # token-threshold flag
        )
        run = await _seed_run(db_session, sd.id)

        ident = _identifier_mock(
            [
                _llm_candidate([blocks[0].id], title="thin1"),
                _llm_candidate([blocks[0].id], title="thin2"),
            ]
        )

        orch = StageCOrchestrator(db_session, identifier=ident)
        result = await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        # Both candidates flagged on the token threshold.
        assert "insufficient_tokens" in result.flag_counts
        assert result.flag_counts["insufficient_tokens"] == 2
        assert result.candidates_emitted == 2
        assert result.candidates_flagged == 2


# ─── Content-block creation helper ────────────────────────────────────────


class TestEnsureContentBlocks:
    async def test_creates_content_blocks_when_missing(self, db_session: AsyncSession) -> None:
        # Seed a source_document + page WITHOUT pre-existing content_blocks.
        sd = SourceDocument(
            title="t",
            source_type="pdf",
            primary_language="en",
            content_domain="clinical",
            original_storage_path="/tmp/x.pdf",
            outline_method="markdown_parser",
            outline_jsonb={"sections": [{"heading": "Sec"}]},
        )
        db_session.add(sd)
        await db_session.flush()
        sp = SourcePage(
            source_document_id=sd.id,
            page_number=1,
            markdown_content="# Heading\n\nSome body content.",
            extraction_method="text",
            extraction_quality_score=0.9,
        )
        db_session.add(sp)
        await db_session.flush()
        await db_session.commit()

        # Confirm: no content_blocks yet.
        before = (
            (
                await db_session.execute(
                    select(ContentBlock).join(SourcePage).where(SourcePage.source_document_id == sd.id)
                )
            )
            .scalars()
            .all()
        )
        assert before == []

        run = await _seed_run(db_session, sd.id)
        # Identifier returns nothing (so we just probe the `_ensure_content_blocks` path).
        ident = _identifier_mock([])
        orch = StageCOrchestrator(db_session, identifier=ident)
        await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        # After Stage C runs, content_blocks have been created.
        after = (
            (
                await db_session.execute(
                    select(ContentBlock).join(SourcePage).where(SourcePage.source_document_id == sd.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(after) >= 1

    async def test_content_blocks_store_plain_text(self, db_session: AsyncSession) -> None:
        sd = SourceDocument(
            title="t",
            source_type="pdf",
            primary_language="en",
            content_domain="clinical",
            original_storage_path="/tmp/x.pdf",
            outline_method="markdown_parser",
            outline_jsonb={"sections": [{"heading": "Sec"}]},
        )
        db_session.add(sd)
        await db_session.flush()
        sp = SourcePage(
            source_document_id=sd.id,
            page_number=1,
            markdown_content="# Heading\n\n**Bold** body with - not a list item.",
            extraction_method="text",
            extraction_quality_score=0.9,
        )
        db_session.add(sp)
        await db_session.flush()
        await db_session.commit()

        run = await _seed_run(db_session, sd.id)
        ident = _identifier_mock([])
        orch = StageCOrchestrator(db_session, identifier=ident)
        await orch.run(ingestion_run_id=run.id, source_document_ids=[sd.id])

        blocks = (
            (
                await db_session.execute(
                    select(ContentBlock).join(SourcePage).where(SourcePage.source_document_id == sd.id)
                )
            )
            .scalars()
            .all()
        )
        paragraph = next(b for b in blocks if b.block_type == "paragraph")
        assert paragraph.content_text == "Bold body with - not a list item."
        assert "**" not in paragraph.content_text
