"""Persistence for the precomputed per-tenant chat feedback summary snapshot."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chat_feedback_summary import ChatFeedbackSummary


class ChatFeedbackSummaryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, tenant_id: uuid.UUID) -> ChatFeedbackSummary | None:
        stmt = select(ChatFeedbackSummary).where(ChatFeedbackSummary.tenant_id == tenant_id)
        return (await self._session.execute(stmt)).scalars().first()

    async def get_computed_at(self, tenant_id: uuid.UUID) -> datetime | None:
        row = await self.get(tenant_id)
        return row.computed_at if row is not None else None

    async def get_payload(self, tenant_id: uuid.UUID) -> dict[str, Any] | None:
        row = await self.get(tenant_id)
        return row.payload_json if row is not None else None

    async def list_tenant_ids(self) -> list[uuid.UUID]:
        stmt = select(ChatFeedbackSummary.tenant_id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def upsert(
        self,
        *,
        tenant_id: uuid.UUID,
        payload_json: dict[str, Any],
        generated_at: datetime,
        computed_at: datetime,
    ) -> None:
        """Replace the snapshot for one tenant."""
        await self._session.execute(
            delete(ChatFeedbackSummary).where(ChatFeedbackSummary.tenant_id == tenant_id)
        )
        now = datetime.now(UTC)
        self._session.add(
            ChatFeedbackSummary(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                payload_json=payload_json,
                generated_at=generated_at,
                computed_at=computed_at,
                created_at=now,
                updated_at=now,
            )
        )
        await self._session.flush()
