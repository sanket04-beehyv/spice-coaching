"""Tests for nightly chat FAQ worker."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from platform_service.services.chat_faq_aggregator import CandidateQuestion, TenantQuestionCandidates
from platform_service.services.chat_faq_generator import SynthesizedChatFaq
from platform_service.workers.chat_faq_worker import aggregate_chat_faqs_job
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(db_session, "chat_frequent_question")
    yield


@pytest.mark.asyncio
class TestChatFaqWorker:
    async def test_persists_synthesized_faqs(self, db_session: AsyncSession) -> None:
        tenant_id = uuid4()
        faq_id = uuid4()
        synthesized = [
            SynthesizedChatFaq(
                id=faq_id,
                question_localized={"bn": "শিশুর কাশি", "en": "child cough"},
                normalized_question="child cough",
                occurrence_count=5,
                rank=1,
                last_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        ]
        ch_mock = AsyncMock()
        ch_mock.close = lambda: None
        ai_mock = AsyncMock()

        with (
            patch(
                "platform_service.workers.chat_faq_worker.get_clickhouse_client",
                return_value=ch_mock,
            ),
            patch(
                "platform_service.workers.chat_faq_worker.get_ai_client",
                return_value=ai_mock,
            ),
            patch(
                "platform_service.workers.chat_faq_worker.ChatFaqAggregator.fetch_candidates",
                AsyncMock(
                    return_value=[
                        TenantQuestionCandidates(
                            tenant_id=tenant_id,
                            questions=[
                                CandidateQuestion(
                                    text="child cough",
                                    normalized_text="child cough",
                                    occurrence_count=5,
                                    last_seen_at=datetime(2026, 6, 1, tzinfo=UTC),
                                )
                            ],
                        )
                    ]
                ),
            ),
            patch(
                "platform_service.workers.chat_faq_worker.ChatFaqClusterer.cluster",
                AsyncMock(return_value=[]),
            ),
            patch(
                "platform_service.workers.chat_faq_worker.ChatFaqGenerator.synthesize",
                AsyncMock(return_value=synthesized),
            ),
            patch("platform_service.workers.chat_faq_worker.SessionLocal") as session_local,
        ):
            session_local.return_value.__aenter__.return_value = db_session
            session_local.return_value.__aexit__.return_value = None
            summary = await aggregate_chat_faqs_job()

        assert summary["tenants_updated"] == 1
        assert summary["faqs_written"] == 1

        result = await db_session.execute(
            text("SELECT question_localized, occurrence_count, rank FROM chat_frequent_question")
        )
        row = result.one()
        assert row.question_localized["bn"] == "শিশুর কাশি"
        assert row.question_localized.get("en") == "child cough"
        assert row.occurrence_count == 5
        assert row.rank == 1
