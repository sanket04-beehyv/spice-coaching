"""Layer 2 chunk 4 — quiz_generation_worker tests.

Covers:
- Pure-unit: `_target_quiz_size` clamping (below min, above max, in-band)
  and `_format_card_block` text composition.
- Happy path with mocked AIRuntimeClient: writes correct number of rows,
  module_id FK set on every row, question_order sequential, correct_indices
  wraps the LLM's correct_index.
- Idempotent retry: pre-seed N questions for the module → call worker →
  the prior rows are gone, only the new ones remain.
- LLM output shape tolerance: dict-with-questions, top-level array,
  malformed JSON, unexpected shape, non-dict question entries.
- Failure paths (return 0, no DB writes):
  - module not found
  - module with no cards
  - module with null module_json
  - LLM error response
  - LLM empty payload
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    InferenceRequest,
    InferenceResponse,
    TokenUsage,
)
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.workers.quiz_generation_worker import (
    _format_card_block,
    _target_quiz_size,
    generate_quiz_for_module,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

# ─── Pure unit: target quiz size + card block formatter ────────────────────


class TestTargetQuizSize:
    def test_one_card_clamps_to_quiz_min(self) -> None:
        from platform_service.config import get_settings

        s = get_settings()
        assert _target_quiz_size(1) == s.quiz_min_questions

    def test_card_count_in_band_returns_card_count(self) -> None:
        from platform_service.config import get_settings

        s = get_settings()
        # Pick something between min and max.
        n = s.quiz_min_questions + 1
        assert _target_quiz_size(n) == n

    def test_many_cards_clamps_to_quiz_max(self) -> None:
        from platform_service.config import get_settings

        s = get_settings()
        assert _target_quiz_size(100) == s.quiz_max_questions

    def test_zero_cards_clamps_to_min(self) -> None:
        from platform_service.config import get_settings

        s = get_settings()
        # min(0, max) = 0; max(min, 0) = min — so floor is min.
        assert _target_quiz_size(0) == s.quiz_min_questions


class TestFormatCardBlock:
    def test_includes_only_populated_fields(self) -> None:
        block = _format_card_block({"title_bn": "T", "body_bn": "B"}, idx=1)
        assert "### Card 1" in block
        assert "Title (bn): T" in block
        assert "Body (bn): B" in block
        # next_action / previous_practice etc not present.
        assert "Next action" not in block
        assert "Rationale" not in block

    def test_includes_all_optional_fields_in_order(self) -> None:
        block = _format_card_block(
            {
                "title_bn": "t",
                "body_bn": "b",
                "next_action_bn": "n",
                "previous_practice_bn": "p",
                "current_practice_bn": "c",
                "rationale_for_change_bn": "r",
            },
            idx=2,
        )
        # Title before body, body before next, next before previous practice,
        # previous before current, current before rationale.
        positions = [
            block.index("Title (bn)"),
            block.index("Body (bn)"),
            block.index("Next action (bn)"),
            block.index("Previous practice (bn)"),
            block.index("Current practice (bn)"),
            block.index("Rationale (bn)"),
        ]
        assert positions == sorted(positions)

    def test_card_index_in_header(self) -> None:
        assert "### Card 7" in _format_card_block({"title_bn": "x"}, idx=7)

    def test_empty_card_only_emits_header(self) -> None:
        block = _format_card_block({}, idx=1)
        assert block.strip() == "### Card 1"


# ─── DB-backed setup ────────────────────────────────────────────────────────


pytestmark_db = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text("TRUNCATE module_quiz_question, module, module_family RESTART IDENTITY CASCADE")
    )
    await db_session.commit()


async def _seed_module(
    session: AsyncSession,
    *,
    cards: list[dict] | None = None,
    title_bn: str = "Module title",
    module_json_override: dict | None = None,
) -> UUID:
    fam = ModuleFamily(module_code=f"f-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    if module_json_override is not None:
        module_json = module_json_override
    elif cards is None:
        module_json = {"cards": [{"title_bn": "c1", "body_bn": "b1"}]}
    else:
        module_json = {"cards": cards}
    module = Module(
        module_family_id=fam.id,
        version=1,
        title_bn=title_bn,
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        module_json=module_json,
        published_at=datetime.now(UTC),
    )
    session.add(module)
    await session.flush()
    fam.current_published_module_id = module.id
    await session.commit()
    return module.id


def _llm_response(payload: Any, *, error: str | None = None) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-1",
        generation_type=GenerationType.QUIZ_DRAFTING,
        provider="google",
        model="gemini-2.5-flash",
        raw_text="" if payload is None else (payload if isinstance(payload, str) else ""),
        parsed_json=payload if not isinstance(payload, str) else None,
        latency_ms=200,
        token_usage=TokenUsage(input=100, output=200),
        error=error,
    )


def _valid_question(idx: int = 0) -> dict[str, Any]:
    return {
        "question_bn": f"Q{idx}",
        "question_en": f"Question {idx}",
        "case_setup_bn": "case bn",
        "case_setup_en": "case en",
        "options_bn": ["a", "b", "c", "d"],
        "options_en": ["A", "B", "C", "D"],
        "correct_index": 1,
        "explanation_bn": "because",
        "explanation_en": "because",
        "primary_card_index": 1,
        "difficulty": "moderate",
    }


@pytest.fixture
def mock_generate(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Patch AIRuntimeClient.generate in the worker's namespace."""
    gen_mock = AsyncMock()

    class _StubClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None: ...

        async def generate(self, request: InferenceRequest) -> InferenceResponse:
            return await gen_mock(request)

    monkeypatch.setattr("platform_service.workers.quiz_generation_worker.AIRuntimeClient", _StubClient)
    return gen_mock


# ─── Happy path ─────────────────────────────────────────────────────────────


class TestHappyPath:
    pytestmark = pytestmark_db

    async def test_writes_questions_with_module_fk(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session, cards=[{"title_bn": f"c{i}"} for i in range(3)])
        mock_generate.return_value = _llm_response({"questions": [_valid_question(i) for i in range(3)]})

        n = await generate_quiz_for_module(module_id)
        assert n == 3

        result = await db_session.execute(
            select(ModuleQuizQuestion)
            .where(ModuleQuizQuestion.module_id == module_id)
            .order_by(ModuleQuizQuestion.question_order)
        )
        rows = list(result.scalars().all())
        assert len(rows) == 3
        # Every row has the FK set.
        assert all(r.module_id == module_id for r in rows)

    async def test_question_order_assigned_sequentially(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session, cards=[{"title_bn": f"c{i}"} for i in range(3)])
        mock_generate.return_value = _llm_response(
            {"questions": [_valid_question(0), _valid_question(1), _valid_question(2)]}
        )

        await generate_quiz_for_module(module_id)
        result = await db_session.execute(
            select(ModuleQuizQuestion)
            .where(ModuleQuizQuestion.module_id == module_id)
            .order_by(ModuleQuizQuestion.question_order)
        )
        orders = [r.question_order for r in result.scalars().all()]
        assert orders == [1, 2, 3]

    async def test_correct_index_wrapped_into_indices_array(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        q = _valid_question(0)
        q["correct_index"] = 2  # LLM emits a single int
        mock_generate.return_value = _llm_response({"questions": [q]})

        await generate_quiz_for_module(module_id)
        result = await db_session.execute(
            select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == module_id)
        )
        row = result.scalar_one()
        # DB stores list[int] (multi-select-capable column).
        assert row.correct_indices == [2]

    async def test_default_correct_index_is_zero(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        q = _valid_question(0)
        del q["correct_index"]  # missing — worker defaults to 0
        mock_generate.return_value = _llm_response({"questions": [q]})

        await generate_quiz_for_module(module_id)
        result = await db_session.execute(
            select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == module_id)
        )
        row = result.scalar_one()
        assert row.correct_indices == [0]


# ─── Idempotent retry ───────────────────────────────────────────────────────


class TestIdempotentRetry:
    pytestmark = pytestmark_db

    async def test_prior_rows_deleted_before_new_writes(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session, cards=[{"title_bn": "c1"}])
        # Pre-seed some "old" quiz rows for this module.
        for i in range(3):
            db_session.add(
                ModuleQuizQuestion(
                    module_id=module_id,
                    question_order=i + 1,
                    question_family_id=uuid4(),
                    question_version=1,
                    question_bn=f"OLD Q{i}",
                    options_bn=["a", "b", "c", "d"],
                    correct_indices=[0],
                )
            )
        await db_session.commit()

        # New LLM response: only 2 questions.
        mock_generate.return_value = _llm_response({"questions": [_valid_question(0), _valid_question(1)]})

        n = await generate_quiz_for_module(module_id)
        assert n == 2

        # The old 3 are gone; only the new 2 remain.
        result = await db_session.execute(
            select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == module_id)
        )
        rows = list(result.scalars().all())
        assert len(rows) == 2
        # And none of the old ones survived.
        for row in rows:
            assert "OLD" not in row.question_bn


# ─── LLM output shape tolerance ─────────────────────────────────────────────


class TestLlmOutputShapes:
    pytestmark = pytestmark_db

    async def test_dict_with_questions_key(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        mock_generate.return_value = _llm_response({"questions": [_valid_question(0)]})
        n = await generate_quiz_for_module(module_id)
        assert n == 1

    async def test_top_level_array_accepted(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        mock_generate.return_value = _llm_response([_valid_question(0), _valid_question(1)])
        n = await generate_quiz_for_module(module_id)
        assert n == 2

    async def test_malformed_raw_json_returns_zero(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        # parsed_json=None forces fallback to raw_text json.loads, which fails.
        response = InferenceResponse(
            request_id="r",
            generation_type=GenerationType.QUIZ_DRAFTING,
            provider="google",
            model="gemini-2.5-flash",
            raw_text="this is not json{{{",
            parsed_json=None,
            latency_ms=10,
            token_usage=TokenUsage(input=0, output=0),
        )
        mock_generate.return_value = response
        n = await generate_quiz_for_module(module_id)
        assert n == 0

    async def test_falls_back_to_raw_text_when_parsed_none(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        response = InferenceResponse(
            request_id="r",
            generation_type=GenerationType.QUIZ_DRAFTING,
            provider="google",
            model="gemini-2.5-flash",
            raw_text=json.dumps({"questions": [_valid_question(0)]}),
            parsed_json=None,
            latency_ms=10,
            token_usage=TokenUsage(input=0, output=0),
        )
        mock_generate.return_value = response
        n = await generate_quiz_for_module(module_id)
        assert n == 1

    async def test_unexpected_payload_shape_returns_zero(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        # parsed_json is a bare integer — neither dict nor list.
        response = InferenceResponse(
            request_id="r",
            generation_type=GenerationType.QUIZ_DRAFTING,
            provider="google",
            model="gemini-2.5-flash",
            raw_text="42",
            parsed_json=None,
            latency_ms=10,
            token_usage=TokenUsage(input=0, output=0),
        )
        mock_generate.return_value = response
        n = await generate_quiz_for_module(module_id)
        assert n == 0

    async def test_non_dict_question_entries_filtered_out(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        mock_generate.return_value = _llm_response(
            {"questions": [_valid_question(0), "not a dict", 42, _valid_question(1)]}
        )
        n = await generate_quiz_for_module(module_id)
        assert n == 2  # only the two dict entries kept


# ─── Failure paths ─────────────────────────────────────────────────────────


class TestFailureReturnZero:
    pytestmark = pytestmark_db

    async def test_module_not_found_returns_zero(self, mock_generate: AsyncMock) -> None:
        n = await generate_quiz_for_module(uuid4())
        assert n == 0
        mock_generate.assert_not_awaited()

    async def test_module_with_no_cards_returns_zero(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session, cards=[])  # empty cards array
        n = await generate_quiz_for_module(module_id)
        assert n == 0
        mock_generate.assert_not_awaited()

    async def test_module_with_null_module_json_returns_zero(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        # Build a module with module_json=None directly.
        fam = ModuleFamily(module_code=f"f-{uuid4().hex[:8]}")
        db_session.add(fam)
        await db_session.flush()
        module = Module(
            module_family_id=fam.id,
            version=1,
            title_bn="t",
            domain="rmnch",
            module_type="refresher",
            lifecycle_status="published",
            module_json=None,
            published_at=datetime.now(UTC),
        )
        db_session.add(module)
        await db_session.commit()

        n = await generate_quiz_for_module(module.id)
        assert n == 0
        mock_generate.assert_not_awaited()

    async def test_llm_error_response_returns_zero(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        mock_generate.return_value = _llm_response(None, error="vertex 503")
        n = await generate_quiz_for_module(module_id)
        assert n == 0

    async def test_llm_empty_questions_returns_zero(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        mock_generate.return_value = _llm_response({"questions": []})
        n = await generate_quiz_for_module(module_id)
        assert n == 0


# ─── Request shape ──────────────────────────────────────────────────────────


class TestRequestShape:
    pytestmark = pytestmark_db

    async def test_uses_quiz_drafting_generation_type_with_json_constraint(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(db_session)
        mock_generate.return_value = _llm_response({"questions": [_valid_question(0)]})

        await generate_quiz_for_module(module_id)
        request: InferenceRequest = mock_generate.call_args.args[0]
        assert request.generation_type == GenerationType.QUIZ_DRAFTING
        assert request.constraints.output_format == "json"
        assert request.constraints.language == "bn"

    async def test_human_message_includes_card_blocks(
        self,
        db_session: AsyncSession,
        mock_generate: AsyncMock,
    ) -> None:
        module_id = await _seed_module(
            db_session,
            cards=[
                {"title_bn": "card-one-title", "body_bn": "card-one-body"},
                {"title_bn": "card-two-title"},
            ],
        )
        mock_generate.return_value = _llm_response({"questions": [_valid_question(0)]})

        await generate_quiz_for_module(module_id)
        request: InferenceRequest = mock_generate.call_args.args[0]
        human = request.prompt.resolved_human_message
        assert "### Card 1" in human
        assert "### Card 2" in human
        assert "card-one-title" in human
        assert "card-two-title" in human
