"""Tests for bilingual chat FAQ LLM synthesis."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, TokenUsage
from platform_service.services.chat_faq_aggregator import CandidateQuestion
from platform_service.services.chat_faq_clusterer import QuestionCluster
from platform_service.services.chat_faq_generator import ChatFaqGenerator


def _cluster(text: str, count: int) -> QuestionCluster:
    member = CandidateQuestion(
        text=text,
        normalized_text=text,
        occurrence_count=count,
        last_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    cluster = QuestionCluster()
    cluster.add_member(member, [1.0, 0.0])
    return cluster


def _inference_response(
    *,
    parsed_json: dict | None = None,
    error: str | None = None,
) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-faq",
        generation_type=GenerationType.CHAT_FAQ_SYNTHESIS,
        provider="openai",
        model="gpt-4o-mini",
        raw_text="",
        parsed_json=parsed_json,
        latency_ms=1,
        token_usage=TokenUsage(input=1, output=1),
        error=error,
    )


@pytest.mark.asyncio
class TestChatFaqGenerator:
    async def test_synthesizes_bilingual_faqs_from_llm(self) -> None:
        tenant_id = uuid4()
        clusters = [_cluster("child cough", 5), _cluster("fever", 3)]
        ai_mock = AsyncMock()
        ai_mock.generate.return_value = _inference_response(
            parsed_json={
                "faqs": [
                    {
                        "question": {"bn": "শিশুর কাশি", "en": "Child cough"},
                        "source_cluster_index": 0,
                    },
                    {
                        "question": {"bn": "জ্বর", "en": "Fever"},
                        "source_cluster_index": 1,
                    },
                ]
            },
        )

        results = await ChatFaqGenerator(client=ai_mock).synthesize(tenant_id, clusters)
        assert len(results) == 2
        assert results[0].question_localized["bn"] == "শিশুর কাশি"
        assert results[0].occurrence_count == 5
        assert results[0].rank == 1

    async def test_falls_back_to_seed_text_on_llm_error(self) -> None:
        tenant_id = uuid4()
        clusters = [_cluster("child cough", 5)]
        ai_mock = AsyncMock()
        ai_mock.generate.return_value = _inference_response(error="provider down")

        results = await ChatFaqGenerator(client=ai_mock).synthesize(tenant_id, clusters)
        assert len(results) == 1
        assert results[0].question_localized["bn"] == "child cough"
