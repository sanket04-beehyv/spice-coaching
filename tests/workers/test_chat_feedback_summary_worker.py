"""Tests for weekly chat feedback summary worker."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from mc_contracts.chat_feedback_summary import ChatFeedbackEventCounts, ChatFeedbackSummaryResponse
from platform_service.services.chat_feedback_aggregator import FeedbackEvent, TenantFeedbackBatch
from platform_service.workers.chat_feedback_summary_worker import aggregate_chat_feedback_summary_job
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(text("TRUNCATE chat_feedback_summary RESTART IDENTITY CASCADE"))
    await db_session.commit()


@pytest.mark.asyncio
class TestChatFeedbackSummaryWorker:
    async def test_persists_synthesized_summary(self, db_session: AsyncSession) -> None:
        tenant_id = uuid4()
        computed_at = datetime(2026, 6, 2, tzinfo=UTC)
        batch = TenantFeedbackBatch(
            tenant_id=tenant_id,
            events=[
                FeedbackEvent(
                    event_id="evt-1",
                    tenant_id=tenant_id,
                    chw_id=1,
                    event_type="chat_feedback_negative",
                    inference_mode="online",
                    module_id=None,
                    question="What is the paracetamol dose?",
                    feedback="Wrong dose",
                    answer_excerpt="Take 500mg",
                    occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
                )
            ],
        )
        synthesized = ChatFeedbackSummaryResponse(
            generated_at=computed_at,
            period_start=None,
            period_end=computed_at,
            event_counts=ChatFeedbackEventCounts(
                positive=0,
                positive_online=0,
                positive_offline=0,
                negative_online=1,
                negative_offline=0,
                total=1,
            ),
            llm_summary="Review online dosing answers.",
            positive_online_themes=[],
            positive_offline_themes=[],
            negative_online_recommendations=["Check retrieval for medication modules."],
            negative_offline_recommendations=[],
        )
        ch_mock = AsyncMock()
        ch_mock.close = lambda: None

        with (
            patch(
                "platform_service.workers.chat_feedback_summary_worker.get_clickhouse_client",
                return_value=ch_mock,
            ),
            patch(
                "platform_service.workers.chat_feedback_summary_worker.get_ai_client",
                return_value=AsyncMock(),
            ),
            patch(
                "platform_service.workers.chat_feedback_summary_worker.ChatFeedbackAggregator.distinct_tenant_ids",
                AsyncMock(return_value=[tenant_id]),
            ),
            patch(
                "platform_service.workers.chat_feedback_summary_worker.ChatFeedbackAggregator.fetch_since",
                AsyncMock(return_value=batch),
            ),
            patch(
                "platform_service.workers.chat_feedback_summary_worker.ChatFeedbackSummaryGenerator.synthesize",
                AsyncMock(return_value=synthesized),
            ),
            patch("platform_service.workers.chat_feedback_summary_worker.SessionLocal") as session_local,
        ):
            session_local.return_value.__aenter__.return_value = db_session
            session_local.return_value.__aexit__.return_value = None
            summary = await aggregate_chat_feedback_summary_job()

        assert summary["tenants_updated"] == 1
        assert summary["tenants_skipped"] == 0

        result = await db_session.execute(
            text("SELECT payload_json FROM chat_feedback_summary WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant_id},
        )
        row = result.one()
        assert row.payload_json["llm_summary"] == "Review online dosing answers."
        assert row.payload_json["negative_online_recommendations"] == [
            "Check retrieval for medication modules."
        ]
