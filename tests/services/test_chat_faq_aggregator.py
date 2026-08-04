"""Tests for chat FAQ candidate fetch from ClickHouse telemetry."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from platform_service.services.chat_faq_aggregator import (
    ChatFaqAggregator,
    normalize_question,
    stable_faq_id,
)
from platform_service.services.question_text import normalize_question as normalize_question_shared


class TestNormalizeQuestion:
    def test_collapses_whitespace(self) -> None:
        assert normalize_question("  child   cough  ") == "child cough"

    def test_casefolds(self) -> None:
        assert normalize_question("How Do I Measure?") == "how do i measure?"

    def test_reexport_matches_shared(self) -> None:
        assert normalize_question is normalize_question_shared


class TestStableFaqId:
    def test_is_deterministic(self) -> None:
        tenant_id = uuid4()
        normalized = "child cough"
        first = stable_faq_id(tenant_id=tenant_id, normalized_question_en=normalized)
        second = stable_faq_id(tenant_id=tenant_id, normalized_question_en=normalized)
        assert first == second


@pytest.mark.asyncio
class TestChatFaqAggregator:
    async def test_groups_candidates_per_tenant(self) -> None:
        tenant_a = uuid4()
        tenant_b = uuid4()
        ch_mock = AsyncMock()
        ch_mock.query_rows.return_value = [
            {
                "tenant_id": str(tenant_a),
                "question_text": "child cough",
                "occurrence_count": 5,
                "last_seen_at": datetime(2026, 6, 1, tzinfo=UTC),
            },
            {
                "tenant_id": str(tenant_a),
                "question_text": "  fever  ",
                "occurrence_count": 3,
                "last_seen_at": datetime(2026, 6, 2, tzinfo=UTC),
            },
            {
                "tenant_id": str(tenant_b),
                "question_text": "blood pressure",
                "occurrence_count": 4,
                "last_seen_at": datetime(2026, 6, 3, tzinfo=UTC),
            },
        ]

        batches = await ChatFaqAggregator(ch_mock).fetch_candidates()
        by_tenant: dict[UUID, list[str]] = {
            batch.tenant_id: [q.text for q in batch.questions] for batch in batches
        }

        assert by_tenant[tenant_a] == ["child cough", "fever"]
        assert by_tenant[tenant_b] == ["blood pressure"]

    async def test_skips_short_questions(self) -> None:
        tenant_id = uuid4()
        ch_mock = AsyncMock()
        ch_mock.query_rows.return_value = [
            {
                "tenant_id": str(tenant_id),
                "question_text": "hi",
                "occurrence_count": 10,
                "last_seen_at": None,
            },
        ]

        batches = await ChatFaqAggregator(ch_mock).fetch_candidates()
        assert batches == []
