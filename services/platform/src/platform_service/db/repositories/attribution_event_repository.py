"""Append-only writes for ``attribution_event``."""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.attribution_event import AttributionEvent


class AttributionEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        event_type: str,
        actor: str,
        source_document_id: UUID | None = None,
        module_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AttributionEvent:
        row = AttributionEvent(
            event_type=event_type,
            actor=actor,
            source_document_id=source_document_id,
            module_id=module_id,
            payload_jsonb=payload,
        )
        self._session.add(row)
        # Scope the flush to the audit row only. Without ``objects=[row]``,
        # ``session.flush()`` walks the entire unit-of-work — meaning a
        # constraint violation on this insert (inside the
        # ``record_attribution_event`` savepoint) would also roll back
        # other pending dirty objects the caller had not yet flushed.
        # Scoped flush keeps the savepoint protection meaningful even
        # when callers lazy-flush.
        await self._session.flush([row])
        return row
