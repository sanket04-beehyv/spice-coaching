"""Tests for chat feedback event fetch and parsing from ClickHouse telemetry."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from platform_service.config import Settings
from platform_service.services.chat_feedback_aggregator import (
    ChatFeedbackAggregator,
    FeedbackEvent,
    TenantFeedbackBatch,
    _extract_answer_excerpt,
    _extract_feedback,
    _extract_question,
    is_online_inference_mode,
)


class TestPayloadExtraction:
    def test_extract_feedback_from_payload(self) -> None:
        payload = {"feedback": "  not helpful  ", "response": {"answer": "Try again"}}
        assert _extract_feedback(payload) == "not helpful"

    def test_extract_question_from_payload(self) -> None:
        payload = {
            "question": "  How do I check respiratory rate?  ",
            "feedback": "helpful",
        }
        assert _extract_question(payload) == "How do I check respiratory rate?"

    def test_extract_question_truncates_long_text(self) -> None:
        payload = {"question": "x" * 400}
        result = _extract_question(payload)
        assert result is not None
        assert len(result) <= 300

    def test_extract_answer_excerpt_from_nested_response(self) -> None:
        payload = {"feedback": "bad", "response": {"answer": "Measure respiratory rate slowly."}}
        assert _extract_answer_excerpt(payload) == "Measure respiratory rate slowly."

    def test_extract_answer_excerpt_from_string_response(self) -> None:
        payload = {
            "feedback": "bad",
            "response": json.dumps({"answer": "Offline cached answer"}),
        }
        assert _extract_answer_excerpt(payload) == "Offline cached answer"


class TestInferenceModeBucketing:
    def test_online_mode(self) -> None:
        assert is_online_inference_mode("online") is True
        assert is_online_inference_mode("ONLINE") is True

    def test_offline_modes(self) -> None:
        assert is_online_inference_mode("edge") is False
        assert is_online_inference_mode(None) is False

    def test_event_bucketing(self) -> None:
        tenant_id = uuid4()
        now = datetime.now(UTC)
        events = [
            FeedbackEvent(
                event_id="e1",
                tenant_id=tenant_id,
                chw_id=1,
                event_type="chat_feedback_positive",
                inference_mode="online",
                module_id=None,
                question="How to measure BP?",
                feedback="helpful",
                answer_excerpt="answer",
                occurred_at=now,
            ),
            FeedbackEvent(
                event_id="e2",
                tenant_id=tenant_id,
                chw_id=1,
                event_type="chat_feedback_positive",
                inference_mode="edge",
                module_id=None,
                question="ANC visit steps?",
                feedback="good offline answer",
                answer_excerpt="answer",
                occurred_at=now,
            ),
            FeedbackEvent(
                event_id="e3",
                tenant_id=tenant_id,
                chw_id=1,
                event_type="chat_feedback_negative",
                inference_mode="online",
                module_id=None,
                question="Dose for paracetamol?",
                feedback="wrong dose",
                answer_excerpt="answer",
                occurred_at=now,
            ),
            FeedbackEvent(
                event_id="e4",
                tenant_id=tenant_id,
                chw_id=1,
                event_type="chat_feedback_negative",
                inference_mode="edge",
                module_id=None,
                question="Offline cache miss?",
                feedback="offline miss",
                answer_excerpt="answer",
                occurred_at=now,
            ),
        ]
        batch = TenantFeedbackBatch(tenant_id=tenant_id, events=events)
        counts = batch.event_counts()
        assert counts == {
            "positive": 2,
            "positive_online": 1,
            "positive_offline": 1,
            "negative_online": 1,
            "negative_offline": 1,
            "total": 4,
        }

    def test_sample_for_llm_respects_caps(self) -> None:
        tenant_id = uuid4()
        now = datetime.now(UTC)
        events = [
            FeedbackEvent(
                event_id=f"e{i}",
                tenant_id=tenant_id,
                chw_id=1,
                event_type="chat_feedback_negative",
                inference_mode="online",
                module_id=None,
                question=f"Question {i}",
                feedback=f"comment {i}",
                answer_excerpt=None,
                occurred_at=now,
            )
            for i in range(5)
        ]
        batch = TenantFeedbackBatch(tenant_id=tenant_id, events=events)
        sampled = batch.sample_for_llm(
            max_positive_online=2,
            max_positive_offline=2,
            max_negative_online=2,
            max_negative_offline=2,
        )
        assert len(sampled) == 2


@pytest.mark.asyncio
class TestChatFeedbackAggregator:
    async def test_fetch_since_parses_rows(self) -> None:
        tenant_id = uuid4()
        module_id = uuid4()
        occurred_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        ch_mock = AsyncMock()
        ch_mock.query_rows.return_value = [
            {
                "id": "evt-1",
                "tenant_id": str(tenant_id),
                "chw_id": 42,
                "event_type": "chat_feedback_negative",
                "inference_mode": "online",
                "module_id": str(module_id),
                "payload_json": json.dumps(
                    {
                        "question": "How do I check respiratory rate?",
                        "feedback": "Incorrect advice",
                        "response": {"answer": "Give aspirin immediately"},
                    }
                ),
                "timestamp_utc": occurred_at,
            }
        ]

        batch = await ChatFeedbackAggregator(ch_mock).fetch_since(
            tenant_id,
            since_ts=datetime(2026, 5, 1, tzinfo=UTC),
        )
        assert len(batch.events) == 1
        event = batch.events[0]
        assert event.question == "How do I check respiratory rate?"
        assert event.feedback == "Incorrect advice"
        assert event.answer_excerpt == "Give aspirin immediately"
        assert event.module_id == module_id

    def test_resolve_since_ts_uses_watermark_when_present(self) -> None:
        watermark = datetime(2026, 6, 1, tzinfo=UTC)
        aggregator = ChatFeedbackAggregator(AsyncMock())
        assert aggregator.resolve_since_ts(watermark=watermark) == watermark

    def test_resolve_since_ts_uses_lookback_on_first_run(self) -> None:
        now = datetime(2026, 6, 10, tzinfo=UTC)
        settings = Settings(chat_feedback_summary_first_run_lookback_days=7)
        aggregator = ChatFeedbackAggregator(AsyncMock(), settings=settings)
        since = aggregator.resolve_since_ts(watermark=None, now=now)
        assert since == now - timedelta(days=7)
