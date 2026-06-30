"""Tests for chat FAQ repository upsert and stale-row deletion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from platform_service.db.repositories.chat_faq_repository import ChatFaqRepository, ChatFaqRow
from platform_service.services.chat_faq_aggregator import stable_faq_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(text("TRUNCATE chat_frequent_question RESTART IDENTITY CASCADE"))
    await db_session.commit()


class TestChatFaqRepository:
    async def test_replace_tenant_faqs_upserts_and_deletes_stale(self, db_session: AsyncSession) -> None:
        tenant_id = uuid4()
        computed_at = datetime.now(UTC)
        repo = ChatFaqRepository(db_session)
        old_question_en = "old question text"
        new_question_en = "new question text"
        old_id = stable_faq_id(tenant_id=tenant_id, normalized_question_en=old_question_en)
        new_id = stable_faq_id(tenant_id=tenant_id, normalized_question_en=new_question_en)

        await repo.replace_tenant_faqs(
            tenant_id,
            [
                ChatFaqRow(
                    id=old_id,
                    question_localized={"bn": "পুরনো প্রশ্ন", "en": old_question_en},
                    normalized_question=old_question_en,
                    occurrence_count=5,
                    rank=1,
                    last_seen_at=computed_at,
                )
            ],
            computed_at=computed_at,
        )
        await db_session.commit()

        later = computed_at + timedelta(minutes=1)
        await repo.replace_tenant_faqs(
            tenant_id,
            [
                ChatFaqRow(
                    id=new_id,
                    question_localized={"bn": "নতুন প্রশ্ন", "en": new_question_en},
                    normalized_question=new_question_en,
                    occurrence_count=8,
                    rank=1,
                    last_seen_at=later,
                )
            ],
            computed_at=later,
        )
        await db_session.commit()

        rows = await repo.list_updated_since(
            tenant_id=tenant_id,
            since=datetime(1970, 1, 1, tzinfo=UTC),
        )
        assert len(rows) == 1
        assert rows[0].id == new_id
        assert rows[0].question_localized.get("en") == new_question_en
        assert rows[0].question_localized["bn"] == "নতুন প্রশ্ন"

    async def test_list_updated_since_filters_by_timestamp(self, db_session: AsyncSession) -> None:
        tenant_id = uuid4()
        computed_at = datetime.now(UTC)
        repo = ChatFaqRepository(db_session)
        question_en = "child cough more than 14 days"
        faq_id = stable_faq_id(tenant_id=tenant_id, normalized_question_en=question_en)

        await repo.replace_tenant_faqs(
            tenant_id,
            [
                ChatFaqRow(
                    id=faq_id,
                    question_localized={"bn": "১৪ দিনের বেশি কাশি", "en": question_en},
                    normalized_question=question_en,
                    occurrence_count=3,
                    rank=1,
                    last_seen_at=computed_at,
                )
            ],
            computed_at=computed_at,
        )
        await db_session.commit()

        before = computed_at - timedelta(seconds=1)
        assert await repo.list_updated_since(tenant_id=tenant_id, since=before)
        assert not await repo.list_updated_since(tenant_id=tenant_id, since=computed_at)

    async def test_list_updated_since_without_tenant_returns_all_tenants(
        self, db_session: AsyncSession
    ) -> None:
        tenant_a = uuid4()
        tenant_b = uuid4()
        computed_at = datetime.now(UTC)
        repo = ChatFaqRepository(db_session)

        for tenant_id, question_en in [
            (tenant_a, "question for tenant a"),
            (tenant_b, "question for tenant b"),
        ]:
            faq_id = stable_faq_id(tenant_id=tenant_id, normalized_question_en=question_en)
            await repo.replace_tenant_faqs(
                tenant_id,
                [
                    ChatFaqRow(
                        id=faq_id,
                        question_localized={"bn": "প্রশ্ন", "en": question_en},
                        normalized_question=question_en,
                        occurrence_count=2,
                        rank=1,
                        last_seen_at=computed_at,
                    )
                ],
                computed_at=computed_at,
            )
        await db_session.commit()

        rows = await repo.list_updated_since(
            tenant_id=None,
            since=datetime(1970, 1, 1, tzinfo=UTC),
        )
        assert len(rows) == 2
        assert {row.question_localized.get("en") for row in rows} == {
            "question for tenant a",
            "question for tenant b",
        }

    async def test_max_computed_at_without_tenant_returns_global_max(self, db_session: AsyncSession) -> None:
        tenant_a = uuid4()
        tenant_b = uuid4()
        earlier = datetime.now(UTC)
        later = earlier + timedelta(hours=1)
        repo = ChatFaqRepository(db_session)

        for tenant_id, computed_at, question_en in [
            (tenant_a, earlier, "earlier question"),
            (tenant_b, later, "later question"),
        ]:
            faq_id = stable_faq_id(tenant_id=tenant_id, normalized_question_en=question_en)
            await repo.replace_tenant_faqs(
                tenant_id,
                [
                    ChatFaqRow(
                        id=faq_id,
                        question_localized={"bn": "প্রশ্ন", "en": question_en},
                        normalized_question=question_en,
                        occurrence_count=1,
                        rank=1,
                        last_seen_at=computed_at,
                    )
                ],
                computed_at=computed_at,
            )
        await db_session.commit()

        assert await repo.max_computed_at(tenant_id=None) == later
        assert await repo.max_computed_at(tenant_id=tenant_a) == earlier
        assert await repo.max_computed_at(tenant_id=tenant_b) == later
