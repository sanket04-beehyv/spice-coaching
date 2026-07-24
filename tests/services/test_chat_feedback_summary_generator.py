"""Tests for chat feedback summary LLM synthesis."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, TokenUsage
from platform_service.config import Settings
from platform_service.services.chat_feedback_aggregator import FeedbackEvent, TenantFeedbackBatch
from platform_service.services.chat_feedback_summary_generator import ChatFeedbackSummaryGenerator
from platform_service.services.prompts.chat_feedback_summary_prompt import fallback_summary


def _make_inference_response(
    *,
    parsed_json: dict | None = None,
    raw_text: str = "",
    error: str | None = None,
) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-test",
        generation_type=GenerationType.CHAT_FEEDBACK_SUMMARY,
        provider="google",
        model="gemini-2.5-flash",
        max_tokens=8192,
        temperature=0.2,
        raw_text=raw_text,
        parsed_json=parsed_json,
        latency_ms=100,
        token_usage=TokenUsage(input=50, output=50),
        error=error,
    )


def _event(
    *,
    event_id: str,
    event_type: str,
    inference_mode: str | None,
    feedback: str,
    question: str | None = "Sample question?",
) -> FeedbackEvent:
    return FeedbackEvent(
        event_id=event_id,
        tenant_id=uuid4(),
        chw_id=1,
        event_type=event_type,
        inference_mode=inference_mode,
        module_id=None,
        question=question,
        feedback=feedback,
        answer_excerpt="Sample answer text",
        occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


class TestFallbackSummary:
    def test_builds_four_bucket_output(self) -> None:
        result = fallback_summary(
            event_counts={
                "positive": 2,
                "positive_online": 1,
                "positive_offline": 1,
                "negative_online": 1,
                "negative_offline": 1,
                "total": 4,
            },
            positive_online_events=[{"feedback": "Very helpful", "question": "Online Q"}],
            positive_offline_events=[{"feedback": "Good offline", "question": "Offline Q"}],
            negative_online_events=[{"feedback": "Wrong protocol", "question": "Online issue"}],
            negative_offline_events=[{"feedback": "Stale content", "question": "Offline issue"}],
        )
        assert "4 new chat feedback events" in result["llm_summary"]
        assert result["positive_online_themes"]
        assert result["positive_offline_themes"]
        assert result["negative_online_recommendations"]
        assert result["negative_offline_recommendations"]

    def test_uses_question_when_feedback_missing(self) -> None:
        result = fallback_summary(
            event_counts={
                "positive": 1,
                "positive_online": 1,
                "positive_offline": 0,
                "negative_online": 0,
                "negative_offline": 0,
                "total": 1,
            },
            positive_online_events=[{"question": "How to measure BP?"}],
            positive_offline_events=[],
            negative_online_events=[],
            negative_offline_events=[],
        )
        assert any("How to measure BP?" in theme for theme in result["positive_online_themes"])


@pytest.mark.asyncio
class TestChatFeedbackSummaryGenerator:
    async def test_uses_llm_json_when_available(self) -> None:
        tenant_id = uuid4()
        batch = TenantFeedbackBatch(
            tenant_id=tenant_id,
            events=[
                _event(
                    event_id="e1",
                    event_type="chat_feedback_negative",
                    inference_mode="online",
                    feedback="Wrong answer",
                )
            ],
        )
        ai_mock = AsyncMock()
        ai_mock.generate.return_value = _make_inference_response(
            parsed_json={
                "llm_summary": "Online RAG needs review.",
                "positive_online_themes": [],
                "positive_offline_themes": [],
                "negative_online_recommendations": ["Improve retrieval for dosing questions."],
                "negative_offline_recommendations": [],
            },
        )
        settings = Settings(chat_feedback_summary_llm_timeout_seconds=5.0)
        generator = ChatFeedbackSummaryGenerator(client=ai_mock, settings=settings)

        result = await generator.synthesize(
            batch=batch,
            period_start=None,
            period_end=datetime(2026, 6, 2, tzinfo=UTC),
        )

        assert result.llm_summary == "Online RAG needs review."
        assert result.negative_online_recommendations == ["Improve retrieval for dosing questions."]
        assert result.event_counts.total == 1

    async def test_falls_back_when_llm_errors(self) -> None:
        tenant_id = uuid4()
        batch = TenantFeedbackBatch(
            tenant_id=tenant_id,
            events=[
                _event(
                    event_id="e1",
                    event_type="chat_feedback_negative",
                    inference_mode="edge",
                    feedback="Offline miss",
                )
            ],
        )
        ai_mock = AsyncMock()
        ai_mock.generate.return_value = _make_inference_response(error="provider down")
        generator = ChatFeedbackSummaryGenerator(client=ai_mock)

        result = await generator.synthesize(
            batch=batch,
            period_start=None,
            period_end=datetime(2026, 6, 2, tzinfo=UTC),
        )

        assert "1 new chat feedback events" in result.llm_summary
        assert result.negative_offline_recommendations

    async def test_includes_previous_summary_in_request_payload(self) -> None:
        from mc_contracts.chat_feedback_summary import (
            ChatFeedbackEventCounts,
            ChatFeedbackSummaryResponse,
        )

        tenant_id = uuid4()
        batch = TenantFeedbackBatch(
            tenant_id=tenant_id,
            events=[
                _event(
                    event_id="e1",
                    event_type="chat_feedback_positive",
                    inference_mode="online",
                    feedback="Great",
                )
            ],
        )
        ai_mock = AsyncMock()
        ai_mock.generate.return_value = _make_inference_response(
            parsed_json={
                "llm_summary": "Updated summary.",
                "positive_online_themes": ["Helpful dosing answers"],
                "positive_offline_themes": [],
                "negative_online_recommendations": [],
                "negative_offline_recommendations": [],
            },
        )
        generator = ChatFeedbackSummaryGenerator(client=ai_mock)
        previous = ChatFeedbackSummaryResponse(
            generated_at=datetime(2026, 5, 1, tzinfo=UTC),
            period_start=None,
            period_end=datetime(2026, 5, 31, tzinfo=UTC),
            event_counts=ChatFeedbackEventCounts(positive=2, positive_online=2, total=2),
            llm_summary="Previous cumulative summary.",
            positive_online_themes=["Fast answers"],
            positive_offline_themes=[],
            negative_online_recommendations=["Old online action"],
            negative_offline_recommendations=[],
        )

        await generator.synthesize(
            batch=batch,
            period_start=datetime(2026, 5, 31, tzinfo=UTC),
            period_end=datetime(2026, 6, 2, tzinfo=UTC),
            previous_summary=previous,
        )

        request = ai_mock.generate.await_args.args[0]
        assert "Previous cumulative summary." in request.prompt.resolved_human_message
        assert "Fast answers" in request.prompt.resolved_human_message
