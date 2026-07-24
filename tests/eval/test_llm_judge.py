"""Unit tests for LLM-as-judge parsing."""

from __future__ import annotations

from uuid import UUID

from eval.rag.corpus import CardCorpusDoc
from eval.rag.llm_judge import build_judge_context, parse_judge_response
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, TokenUsage


def test_build_judge_context_respects_char_budget() -> None:
    module_id = UUID("11111111-1111-1111-1111-111111111111")
    cards_by_module = {
        module_id: [
            CardCorpusDoc(
                module_id=module_id,
                card_id=UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
                card_index=0,
                card_family_id=None,
                primary_title="Card A",
                title_en=None,
                title_bn=None,
                text="x" * 500,
            )
        ]
    }
    context = build_judge_context([module_id], cards_by_module, max_chars=100)
    assert len(context) <= 120
    assert "MODULE" in context


def test_parse_judge_response_from_parsed_json() -> None:
    response = InferenceResponse(
        request_id="req-1",
        generation_type=GenerationType.RAG_EVAL_JUDGE,
        provider="google",
        model="test-model",
        max_tokens=256,
        temperature=0.0,
        raw_text="",
        parsed_json={
            "faithfulness": 0.9,
            "answer_relevance": 1.1,
            "groundedness": -0.2,
        },
        latency_ms=10,
        token_usage=TokenUsage(input=1, output=1),
        error=None,
    )
    scores = parse_judge_response(response)
    assert scores.faithfulness == 0.9
    assert scores.answer_relevance == 1.0
    assert scores.groundedness == 0.0
    assert scores.judge_error is None


def test_parse_judge_response_handles_invalid_json() -> None:
    response = InferenceResponse(
        request_id="req-2",
        generation_type=GenerationType.RAG_EVAL_JUDGE,
        provider="google",
        model="test-model",
        max_tokens=256,
        temperature=0.0,
        raw_text="not json",
        parsed_json=None,
        latency_ms=10,
        token_usage=TokenUsage(input=1, output=1),
        error=None,
    )
    scores = parse_judge_response(response)
    assert scores.faithfulness is None
    assert scores.judge_error is not None
