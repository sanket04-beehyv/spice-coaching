"""Tests for chat feedback summary repository upsert and watermark reads."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from platform_service.db.repositories.chat_feedback_summary_repository import ChatFeedbackSummaryRepository
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


class TestChatFeedbackSummaryRepository:
    async def test_upsert_and_read_watermark(self, db_session: AsyncSession) -> None:
        tenant_id = uuid4()
        computed_at = datetime(2026, 6, 1, tzinfo=UTC)
        repo = ChatFeedbackSummaryRepository(db_session)
        payload = {
            "generated_at": computed_at.isoformat(),
            "period_start": None,
            "period_end": computed_at.isoformat(),
            "event_counts": {
                "positive": 1,
                "positive_online": 1,
                "positive_offline": 0,
                "negative_online": 0,
                "negative_offline": 0,
                "total": 1,
            },
            "llm_summary": "One positive feedback event.",
            "positive_online_themes": ["Helpful"],
            "positive_offline_themes": [],
            "negative_online_recommendations": [],
            "negative_offline_recommendations": [],
            "sampled_events": [],
        }

        await repo.upsert(
            tenant_id=tenant_id,
            payload_json=payload,
            generated_at=computed_at,
            computed_at=computed_at,
        )
        await db_session.commit()

        assert await repo.get_computed_at(tenant_id) == computed_at
        stored = await repo.get_payload(tenant_id)
        assert stored is not None
        assert stored["llm_summary"] == "One positive feedback event."

    async def test_upsert_replaces_existing_snapshot(self, db_session: AsyncSession) -> None:
        tenant_id = uuid4()
        repo = ChatFeedbackSummaryRepository(db_session)
        first_at = datetime(2026, 6, 1, tzinfo=UTC)
        second_at = first_at + timedelta(days=7)

        for computed_at, summary in (
            (first_at, "First summary."),
            (second_at, "Second summary."),
        ):
            await repo.upsert(
                tenant_id=tenant_id,
                payload_json={
                    "generated_at": computed_at.isoformat(),
                    "period_start": None,
                    "period_end": computed_at.isoformat(),
                    "event_counts": {
                        "positive": 0,
                        "positive_online": 0,
                        "positive_offline": 0,
                        "negative_online": 0,
                        "negative_offline": 0,
                        "total": 0,
                    },
                    "llm_summary": summary,
                    "positive_online_themes": [],
                    "positive_offline_themes": [],
                    "negative_online_recommendations": [],
                    "negative_offline_recommendations": [],
                    "sampled_events": [],
                },
                generated_at=computed_at,
                computed_at=computed_at,
            )
            await db_session.commit()

        assert await repo.get_computed_at(tenant_id) == second_at
        stored = await repo.get_payload(tenant_id)
        assert stored is not None
        assert stored["llm_summary"] == "Second summary."
        assert await repo.list_tenant_ids() == [tenant_id]
