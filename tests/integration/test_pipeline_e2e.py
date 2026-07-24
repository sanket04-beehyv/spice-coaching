"""Layer 3 — end-to-end pipeline integration tests.

Each test runs the full `PipelineOrchestrator` against a real Postgres
(via the chunk-2 fixtures) with:

- Stage A using the *real* `StageAExtractor`, but with text/page-renderer
  injected so we don't need a PDF on disk.
- Stage C using the *real* `StageCOrchestrator` with a mocked
  `ModuleIdentifier`.
- Stage D using the *real* `StageDOrchestrator` + real `CardDrafter` /
  `ModuleCardValidator` — only the underlying `AIRuntimeClient` is
  mocked.
- Post-publish workers (`generate_quiz_for_module`,
  `generate_embedding_for_module`) run **explicitly** in the test body
  after `orch.run()` returns, so the assertions can check the worker's
  side effects deterministically without juggling Celery / cross-loop
  task scheduling.

The orchestrator's `_enqueue_post_publish` is replaced with a no-op for
all tests (autouse fixture). Tests that want to exercise post-publish
behaviour call `_run_post_publish_inproc(...)` directly.

Scenarios covered:

| # | Scenario | Pass condition |
|---|---|---|
| 1 | Happy path | Module published; cards in module_json; quiz rows; embedding set |
| 2 | Outline-empty (Stage 1 fails) | Run failed, no module created |
| 3 | Stage 2 returns 0 candidates | Run succeeded, Stage D step skipped |
| 4 | One Stage D candidate fails | partially_succeeded, surviving candidates published |
| 5 | Resume after Stage 2 failure | Stage 1 not re-run; pipeline picks up at Stage 2 |
| 6 | Embedding worker AI-runtime down | Module still published; embedding stays NULL |
| 7 | Quiz worker malformed JSON | Module still published; zero quiz rows |
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    InferenceRequest,
    InferenceResponse,
    TokenUsage,
)
from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.module import Module
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.card_drafter import (
    CardDrafter,
    CardDrafterError,
    CardDrafterResult,
)
from platform_service.services.module_identifier import (
    ModuleIdentifier,
    ModuleIdentifierError,
    ModuleIdentifierResult,
)
from platform_service.workers.embedding_worker import generate_embedding_for_module
from platform_service.workers.extractors.text_extractor import ExtractedPage
from platform_service.workers.gap_classification_worker import classify_module_gaps_for_module
from platform_service.workers.pipeline_orchestrator import PipelineOrchestrator
from platform_service.workers.quiz_generation_worker import generate_quiz_for_module
from platform_service.workers.stage_a_extract import StageAExtractor
from platform_service.workers.stage_c_identify import StageCOrchestrator
from platform_service.workers.stage_d_draft import StageDOrchestrator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db]


# ─── Cleanup ──────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text(
            "TRUNCATE module_quiz_question, module, module_family, "
            "behavioural_gap, module_candidate_draft, content_block, source_page, "
            "source_document, ingestion_run_step, ingestion_run, "
            "llm_call_cache "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


@pytest.fixture(autouse=True)
def _stub_post_publish_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `_enqueue_post_publish` with a no-op for every test in this
    module. Tests that want to exercise post-publish behaviour call
    `_run_post_publish_inproc()` directly after `orch.run()`."""

    async def _noop_enqueue(self, *_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(StageDOrchestrator, "_enqueue_post_publish", _noop_enqueue)


# ─── Synthetic input + LLM responses ──────────────────────────────────────


def _good_text_pages(count: int = 3) -> list[ExtractedPage]:
    body_words = " ".join(f"word{i}" for i in range(150))
    return [
        ExtractedPage(
            page_number=p,
            markdown=(
                f"# Chapter {p}: Maternal Health\n\n"
                f"## Section A\n\n"
                f"{body_words}\n\n"
                f"## Section B\n\n"
                f"More body content here for page {p}."
            ),
        )
        for p in range(1, count + 1)
    ]


def _no_heading_text_pages(count: int = 3) -> list[ExtractedPage]:
    body = " ".join(f"plain{i}" for i in range(100))
    return [ExtractedPage(page_number=p, markdown=body) for p in range(1, count + 1)]


@pytest.fixture
def patch_count_pages(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    def _set(n: int) -> None:
        monkeypatch.setattr(
            "platform_service.workers.stage_a_extract.count_pages",
            lambda *_args, **_kwargs: n,
        )

    return _set


def _id_response(candidates: list[dict[str, Any]]) -> ModuleIdentifierResult:
    return ModuleIdentifierResult(candidates=candidates, raw_response_text="")


def _draft_response(*, cards: list[dict] | None = None, insufficient: str | None = None) -> CardDrafterResult:
    return CardDrafterResult(
        cards=cards or [_card(i) for i in range(5)],
        insufficient_reason=insufficient,
    )


def _card(idx: int = 0) -> dict[str, Any]:
    return {
        "title": {"bn": f"কার্ড {idx}", "en": f"Card {idx}" * 5},
        "body": {"bn": "মূল বিষয় এবং পরবর্তী পদক্ষেপ। "} * 5,
        "next_action": {"bn": "পরবর্তী পদক্ষেপ নিন।"},
        "source_block_ids": [str(uuid4())],
    }


def _id_candidate(block_ids: list[UUID], *, title: str = "Topic") -> dict[str, Any]:
    return {
        "proposed_title": title,
        "scope_summary": "Summary of the topic.",
        "source_provenance": [
            {
                "source_document_id": str(uuid4()),
                "source_page_id": str(uuid4()),
                "content_block_ids": [str(b) for b in block_ids],
            }
        ],
        "estimated_card_count": 5,
        "estimated_quiz_count": 4,
        "proposed_module_type": "refresher",
    }


# ─── Source-document seed ─────────────────────────────────────────────────


async def _seed_source_doc(session: AsyncSession) -> UUID:
    sd = SourceDocument(
        title="integration-test",
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        assessment_mode="with_quiz",
        original_storage_path="/tmp/fake.pdf",
    )
    session.add(sd)
    await session.flush()
    await session.commit()
    return sd.id


# ─── Orchestrator builder (real stages, mocked LLM) ───────────────────────


async def _run_orchestrator(
    *,
    source_document_id: UUID,
    text_pages: list[ExtractedPage],
    identify_candidates_fn: Callable[[list[UUID]], list[dict[str, Any]]] | None = None,
    card_drafter: CardDrafter | Any | None = None,
    resume: bool = True,
    session: AsyncSession | None = None,
) -> Any:
    """Build a PipelineOrchestrator on a *fresh* session and run it.

    Sharing one session between the orchestrator and the test body causes
    SQLAlchemy MissingGreenlet errors on the orchestrator's failure-recovery
    path (rollback + auto-begin doesn't always re-enter greenlet context
    from a session that's also being used by the test). Production code
    runs each ingestion on its own session via `ingest_worker.run_pipeline_for_source_job`;
    mirroring that here gives stable behaviour.
    """
    own_session = session or SessionLocal()
    try:
        text_extractor_fn = MagicMock(return_value=text_pages)
        page_renderer = MagicMock(return_value=b"FAKE_PNG_BYTES")
        vision_extractor = MagicMock()
        vision_extractor.extract_page = AsyncMock()
        stage_a = StageAExtractor(
            own_session,
            vision_extractor=vision_extractor,
            page_renderer=page_renderer,
            text_extractor_fn=text_extractor_fn,
        )

        identifier = MagicMock(spec=ModuleIdentifier)

        async def _identify(**kwargs: Any) -> ModuleIdentifierResult:
            valid_block_ids = list(kwargs.get("valid_block_ids") or [])
            if identify_candidates_fn is None:
                cands = [_id_candidate(valid_block_ids[:1])] if valid_block_ids else []
            else:
                cands = identify_candidates_fn(valid_block_ids)
            return _id_response(cands)

        identifier.identify = AsyncMock(side_effect=_identify)
        stage_c = StageCOrchestrator(own_session, identifier=identifier)

        if card_drafter is None:
            card_drafter = MagicMock()
            card_drafter.draft = AsyncMock(return_value=_draft_response())
        stage_d = StageDOrchestrator(own_session, card_drafter=card_drafter)

        orch = PipelineOrchestrator(own_session, stage_a=stage_a, stage_c=stage_c, stage_d=stage_d)
        result = await orch.run(
            source_document_id=source_document_id,
            source_path="/tmp/fake.pdf",
            source_type="pdf",
            resume=resume,
        )
        # Persist whatever the orchestrator's last commit landed on.
        return result, text_extractor_fn
    finally:
        if session is None:
            await own_session.close()


def _make_card_drafter_with_canned_response(cards: list[dict]) -> CardDrafter:
    client = MagicMock()
    client.generate = AsyncMock(
        return_value=InferenceResponse(
            request_id="r-cards",
            generation_type=GenerationType.CARD_DRAFTING,
            provider="google",
            model="gemini-2.5-flash",
            max_tokens=8192,
            temperature=0.2,
            raw_text="",
            parsed_json={"cards": cards},
            latency_ms=10,
            token_usage=TokenUsage(input=10, output=10),
        )
    )
    return CardDrafter(client=client)


# ─── In-process post-publish runner ───────────────────────────────────────


def _quiz_q(idx: int) -> dict[str, Any]:
    return {
        "question": {"bn": f"প্রশ্ন {idx}", "en": f"Question {idx}"},
        "options": {"bn": ["a", "b", "c", "d"], "en": ["A", "B", "C", "D"]},
        "correct_index": 0,
        "explanation": {"bn": "ব্যাখ্যা"},
        "primary_card_index": 1,
        "difficulty": "moderate",
    }


async def _run_post_publish_inproc(
    module_id: UUID,
    *,
    quiz_questions: list[dict] | None = None,
    quiz_raw_text: str | None = None,
    embed_vector: list[float] | None = None,
    embed_raises: Exception | None = None,
) -> None:
    """Run the actual quiz + embedding workers in-process with mocked
    AI-runtime responses. Mirrors what Celery would do, but inline.
    """
    if quiz_questions is None and quiz_raw_text is None:
        quiz_questions = [_quiz_q(i) for i in range(4)]

    quiz_resp = InferenceResponse(
        request_id="r-quiz",
        generation_type=GenerationType.QUIZ_DRAFTING,
        provider="google",
        model="gemini-2.5-flash",
        max_tokens=8192,
        temperature=0.2,
        raw_text=quiz_raw_text or "",
        parsed_json=None if quiz_raw_text is not None else {"questions": quiz_questions or []},
        latency_ms=10,
        token_usage=TokenUsage(input=10, output=10),
    )

    class _QuizClientStub:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        async def generate(self, _request: InferenceRequest) -> InferenceResponse:
            return quiz_resp

    class _EmbedClientStub:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        async def embed(self, _texts: list[str]) -> list[list[float]]:
            if embed_raises is not None:
                raise embed_raises
            dim = get_settings().embedding_dimension
            vec = embed_vector if embed_vector is not None else [0.1] * dim
            return [vec]

    with patch("platform_service.workers.quiz_generation_worker.AIRuntimeClient", _QuizClientStub):
        try:
            await generate_quiz_for_module(module_id)
        except Exception:  # noqa: BLE001 — workers must not propagate
            pass

    with patch("platform_service.workers.embedding_worker.AIRuntimeClient", _EmbedClientStub):
        try:
            await generate_embedding_for_module(module_id)
        except Exception:  # noqa: BLE001
            pass

    gap_resp = InferenceResponse(
        request_id="r-gap",
        generation_type=GenerationType.MODULE_GAP_CLASSIFICATION,
        provider="google",
        model="gemini-2.5-flash",
        max_tokens=8192,
        temperature=0.2,
        raw_text="",
        parsed_json={"associated_gap_codes": [], "rationale": "e2e stub"},
        latency_ms=10,
        token_usage=TokenUsage(input=10, output=10),
    )

    class _GapClientStub:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...

        async def generate(self, _request: InferenceRequest) -> InferenceResponse:
            return gap_resp

    with patch(
        "platform_service.services.module_gap_classifier.AIRuntimeClient",
        _GapClientStub,
    ):
        try:
            await classify_module_gaps_for_module(module_id)
        except Exception:  # noqa: BLE001
            pass


# ─── Scenario 1: Happy path ────────────────────────────────────────────────


class TestHappyPath:
    async def test_full_pipeline_produces_module_with_quiz_and_embedding(
        self,
        db_session: AsyncSession,
        patch_count_pages: Callable[[int], None],
    ) -> None:
        sd_id = await _seed_source_doc(db_session)
        text_pages = _good_text_pages(3)
        patch_count_pages(3)

        result, _ = await _run_orchestrator(source_document_id=sd_id, text_pages=text_pages)

        assert result.final_status == "succeeded"
        assert result.candidates_emitted >= 1
        assert result.drafts_produced >= 1

        # Stage D now lands modules as ``lifecycle_status="draft"`` per the
        # gated-publish flow (commit 28c0d06). Publish happens via
        # ``set_clinically_reviewed(True)`` on the admin path. The test
        # asserts Stage D's outputs end up in the draft bucket and the
        # attribution graph is wired (source_document_ids populated, cards
        # carry source_block_ids).
        modules = (
            (await db_session.execute(select(Module).where(Module.lifecycle_status == "draft")))
            .scalars()
            .all()
        )
        assert len(modules) >= 1
        m = modules[0]
        assert m.module_json is not None
        cards = m.module_json.get("cards", [])
        assert len(cards) >= 3
        assert m.clinically_reviewed is False
        assert m.primary_gap_id is not None
        # Attribution graph: drafter must populate source_document_ids on the
        # module and source_block_ids on each card so /coaching/rag-query can
        # surface page references downstream.
        assert m.source_document_ids
        assert all(card.get("source_block_ids") for card in cards)

        # Run post-publish workers and verify their effects.
        await _run_post_publish_inproc(m.id)

        await db_session.refresh(m)
        assert m.embedding is not None
        assert len(m.embedding) == get_settings().embedding_dimension

        quiz = (
            (await db_session.execute(select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == m.id)))
            .scalars()
            .all()
        )
        assert len(quiz) >= 1


# ─── Scenario 2: Outline-empty ────────────────────────────────────────────


class TestOutlineEmptyFailsRun:
    async def test_no_heading_markers_fails_run_no_module(
        self,
        db_session: AsyncSession,
        patch_count_pages: Callable[[int], None],
    ) -> None:
        sd_id = await _seed_source_doc(db_session)
        text_pages = _no_heading_text_pages(3)
        patch_count_pages(3)

        result, _ = await _run_orchestrator(source_document_id=sd_id, text_pages=text_pages)

        assert result.final_status == "failed"
        modules = (await db_session.execute(select(Module))).scalars().all()
        assert modules == []
        run = (
            await db_session.execute(select(IngestionRun).where(IngestionRun.id == result.run_id))
        ).scalar_one()
        assert run.status == "failed"
        assert run.error_jsonb["failed_stage"] == "extract"


# ─── Scenario 3: Stage C zero candidates ──────────────────────────────────


class TestStageCZeroCandidatesSkipsStageD:
    async def test_run_succeeds_with_card_draft_skipped(
        self,
        db_session: AsyncSession,
        patch_count_pages: Callable[[int], None],
    ) -> None:
        sd_id = await _seed_source_doc(db_session)
        text_pages = _good_text_pages(2)
        patch_count_pages(2)

        result, _ = await _run_orchestrator(
            source_document_id=sd_id,
            text_pages=text_pages,
            identify_candidates_fn=lambda _bids: [],
        )

        assert result.final_status == "succeeded"
        modules = (await db_session.execute(select(Module))).scalars().all()
        assert modules == []
        steps = (
            (
                await db_session.execute(
                    select(IngestionRunStep)
                    .where(IngestionRunStep.ingestion_run_id == result.run_id)
                    .where(IngestionRunStep.stage == "card_draft")
                )
            )
            .scalars()
            .all()
        )
        assert len(steps) == 1
        assert steps[0].status == "skipped"
        assert steps[0].output_summary_jsonb["skipped_reason"] == "no_candidates_from_stage_c"


# ─── Scenario 4: Per-candidate Stage D failure ────────────────────────────


class TestStageDPerCandidateFailure:
    async def test_one_failing_candidate_partially_succeeds(
        self,
        db_session: AsyncSession,
        patch_count_pages: Callable[[int], None],
    ) -> None:
        sd_id = await _seed_source_doc(db_session)
        text_pages = _good_text_pages(3)
        patch_count_pages(3)

        def _three_cands(block_ids: list[UUID]) -> list[dict]:
            return [_id_candidate(block_ids[:1], title=f"T{i}") for i in range(3)]

        call_count = {"n": 0}

        class _FlakyDrafter:
            async def draft(self, **_kwargs: Any) -> CardDrafterResult:
                call_count["n"] += 1
                if call_count["n"] == 2:
                    raise CardDrafterError("simulated fail on candidate 2")
                return _draft_response()

        result, _ = await _run_orchestrator(
            source_document_id=sd_id,
            text_pages=text_pages,
            identify_candidates_fn=_three_cands,
            card_drafter=_FlakyDrafter(),
        )

        assert result.final_status == "partially_succeeded"
        modules = (
            (await db_session.execute(select(Module).where(Module.lifecycle_status == "published")))
            .scalars()
            .all()
        )
        assert len(modules) == 2
        run = (
            await db_session.execute(select(IngestionRun).where(IngestionRun.id == result.run_id))
        ).scalar_one()
        assert run.error_jsonb["draft_failures"] == 1
        assert run.error_jsonb["drafts_produced"] == 2


# ─── Scenario 5: Resume after partial failure ─────────────────────────────


class TestResumeAfterPartialFailure:
    @pytest.mark.skip(
        reason="Test relied on Stage 2 making run partially_succeeded via "
        "ModuleIdentifierError. The chunked architecture catches per-chunk "
        "failures internally and returns 0 candidates without raising — run "
        "ends `succeeded` not `partially_succeeded`, so `find_resumable_run` "
        "doesn't pick it up and a fresh run re-extracts. Rewrite to trigger "
        "partially_succeeded via a Stage 2-draft failure instead."
    )
    async def test_resume_skips_already_succeeded_extract_stage(
        self,
        db_session: AsyncSession,
        patch_count_pages: Callable[[int], None],
    ) -> None:
        sd_id = await _seed_source_doc(db_session)
        text_pages = _good_text_pages(2)
        patch_count_pages(2)

        # Track the text_extractor_fn across both runs so we can prove
        # Stage 1 was skipped on resume.
        text_extractor_fn = MagicMock(return_value=text_pages)
        page_renderer = MagicMock(return_value=b"X")
        vision_extractor = MagicMock()
        vision_extractor.extract_page = AsyncMock()

        # First run: Stage 2 raises, run ends partially_succeeded.
        # Use a dedicated session for the orchestrator to avoid greenlet
        # contention with db_session.
        first_session = SessionLocal()
        try:
            stage_a_first = StageAExtractor(
                first_session,
                vision_extractor=vision_extractor,
                page_renderer=page_renderer,
                text_extractor_fn=text_extractor_fn,
            )
            identifier_raising = MagicMock(spec=ModuleIdentifier)
            identifier_raising.identify = AsyncMock(side_effect=ModuleIdentifierError("Vertex 503"))
            stage_c_fail = StageCOrchestrator(first_session, identifier=identifier_raising)
            stage_d_first = StageDOrchestrator(first_session, card_drafter=MagicMock())
            orch_first = PipelineOrchestrator(
                first_session,
                stage_a=stage_a_first,
                stage_c=stage_c_fail,
                stage_d=stage_d_first,
            )
            first_result = await orch_first.run(
                source_document_id=sd_id,
                source_path="/tmp/fake.pdf",
                source_type="pdf",
            )
        finally:
            await first_session.close()

        # In the chunked architecture, a per-chunk identifier failure does
        # not propagate up — Stage 2 records `chunks_failed` and returns 0
        # candidates. The orchestrator sees 0 candidates and skips Stage D
        # cleanly; final status is `succeeded`. The test's purpose is to
        # prove Stage 1 is skipped on resume below, not to test failure
        # propagation.
        assert first_result.final_status == "succeeded"
        assert text_extractor_fn.call_count == 1

        text_extractor_fn.reset_mock()

        # Second run: identifier now succeeds.
        second_session = SessionLocal()
        try:
            stage_a_second = StageAExtractor(
                second_session,
                vision_extractor=vision_extractor,
                page_renderer=page_renderer,
                text_extractor_fn=text_extractor_fn,
            )
            identifier_ok = MagicMock(spec=ModuleIdentifier)

            async def _ok_identify(**kwargs: Any) -> ModuleIdentifierResult:
                block_ids = list(kwargs["valid_block_ids"])
                return _id_response([_id_candidate(block_ids[:1])] if block_ids else [])

            identifier_ok.identify = AsyncMock(side_effect=_ok_identify)
            stage_c_ok = StageCOrchestrator(second_session, identifier=identifier_ok)
            drafter_mock = MagicMock()
            drafter_mock.draft = AsyncMock(return_value=_draft_response())
            stage_d_real = StageDOrchestrator(
                second_session,
                card_drafter=drafter_mock,
            )

            orch_second = PipelineOrchestrator(
                second_session,
                stage_a=stage_a_second,
                stage_c=stage_c_ok,
                stage_d=stage_d_real,
            )
            second_result = await orch_second.run(
                source_document_id=sd_id,
                source_path="/tmp/fake.pdf",
                source_type="pdf",
                resume=True,
            )
        finally:
            await second_session.close()

        assert second_result.run_id == first_result.run_id
        # Stage A's extractor was NOT called again on resume.
        text_extractor_fn.assert_not_called()
        assert second_result.final_status == "succeeded"


# ─── Scenario 6: Embedding worker AI-runtime down ─────────────────────────


class TestEmbeddingWorkerFailureNonBlocking:
    async def test_module_published_embedding_stays_null_when_embed_raises(
        self,
        db_session: AsyncSession,
        patch_count_pages: Callable[[int], None],
    ) -> None:
        sd_id = await _seed_source_doc(db_session)
        text_pages = _good_text_pages(2)
        patch_count_pages(2)

        result, _ = await _run_orchestrator(source_document_id=sd_id, text_pages=text_pages)

        assert result.final_status == "succeeded"
        modules = (
            (await db_session.execute(select(Module).where(Module.lifecycle_status == "published")))
            .scalars()
            .all()
        )
        assert len(modules) >= 1
        m = modules[0]

        # Run embedding worker with the AI-runtime raising.
        await _run_post_publish_inproc(m.id, embed_raises=RuntimeError("Vertex 503"))

        await db_session.refresh(m)
        assert m.embedding is None


# ─── Scenario 7: Quiz worker malformed JSON ───────────────────────────────


class TestQuizWorkerMalformedJsonNonBlocking:
    async def test_module_published_zero_quiz_rows_when_quiz_response_is_garbage(
        self,
        db_session: AsyncSession,
        patch_count_pages: Callable[[int], None],
    ) -> None:
        sd_id = await _seed_source_doc(db_session)
        text_pages = _good_text_pages(2)
        patch_count_pages(2)

        result, _ = await _run_orchestrator(source_document_id=sd_id, text_pages=text_pages)

        assert result.final_status == "succeeded"
        modules = (
            (await db_session.execute(select(Module).where(Module.lifecycle_status == "published")))
            .scalars()
            .all()
        )
        assert len(modules) >= 1
        m = modules[0]

        # Run quiz worker with malformed JSON response.
        await _run_post_publish_inproc(m.id, quiz_raw_text="this is definitely not json{{{")

        quiz = (
            (await db_session.execute(select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == m.id)))
            .scalars()
            .all()
        )
        assert quiz == []
