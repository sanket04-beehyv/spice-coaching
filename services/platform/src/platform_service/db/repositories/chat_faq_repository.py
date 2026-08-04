"""Persistence for ranked chat FAQs mined from telemetry."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from mc_contracts.localized import LocalizedString
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chat_frequent_question import ChatFrequentQuestion


@dataclass(frozen=True)
class ChatFaqRow:
    id: uuid.UUID
    question_localized: LocalizedString
    normalized_question: str
    occurrence_count: int
    rank: int
    last_seen_at: datetime | None


class ChatFaqRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_tenant_faqs(
        self,
        tenant_id: uuid.UUID,
        rows: list[ChatFaqRow],
        *,
        computed_at: datetime,
    ) -> None:
        """Upsert the current top-N FAQs and drop questions that fell out of ranking."""
        if not rows:
            await self._session.execute(
                delete(ChatFrequentQuestion).where(
                    ChatFrequentQuestion.tenant_id == tenant_id,
                )
            )
            return

        now = computed_at
        for row in rows:
            stmt = (
                pg_insert(ChatFrequentQuestion)
                .values(
                    id=row.id,
                    tenant_id=tenant_id,
                    question_localized=row.question_localized,
                    normalized_question=row.normalized_question,
                    occurrence_count=row.occurrence_count,
                    rank=row.rank,
                    last_seen_at=row.last_seen_at,
                    computed_at=computed_at,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[
                        ChatFrequentQuestion.tenant_id,
                        ChatFrequentQuestion.normalized_question,
                    ],
                    set_={
                        "question_localized": row.question_localized,
                        "occurrence_count": row.occurrence_count,
                        "rank": row.rank,
                        "last_seen_at": row.last_seen_at,
                        "computed_at": computed_at,
                        "updated_at": now,
                    },
                )
            )
            await self._session.execute(stmt)

        await self._session.execute(
            delete(ChatFrequentQuestion).where(
                ChatFrequentQuestion.tenant_id == tenant_id,
                ChatFrequentQuestion.computed_at < computed_at,
            )
        )

    async def list_updated_since(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
        since: datetime,
    ) -> list[ChatFrequentQuestion]:
        stmt = select(ChatFrequentQuestion).where(ChatFrequentQuestion.updated_at > since)
        if tenant_id is not None:
            stmt = stmt.where(ChatFrequentQuestion.tenant_id == tenant_id)
        stmt = stmt.order_by(ChatFrequentQuestion.rank.asc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def max_computed_at(self, *, tenant_id: uuid.UUID | None = None) -> datetime | None:
        stmt = select(ChatFrequentQuestion.computed_at)
        if tenant_id is not None:
            stmt = stmt.where(ChatFrequentQuestion.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        values = [row[0] for row in result.all()]
        if not values:
            return None
        return max(values)
