"""Best-effort, savepoint-isolated audit writes.

The review on the prior implementation flagged that an audit write that
fails inside the request's active transaction can poison that session —
the outer ``commit()`` then silently rolls back the primary work. This
service writes the audit row inside a SAVEPOINT when the caller already
has a transaction (the request-handler case), so an append failure rolls
back only the savepoint. When the session has no active transaction
(background task with a freshly opened ``SessionLocal``), the write runs
in its own transaction.

Either way, exceptions are caught and logged: audit must never fail the
primary flow.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.attribution_event_repository import (
    AttributionEventRepository,
)

logger = logging.getLogger(__name__)


async def record_attribution_event(
    session: AsyncSession,
    *,
    event_type: str,
    actor: str,
    source_document_id: UUID | None = None,
    module_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append an ``attribution_event`` row without poisoning the caller's transaction."""
    try:
        if session.in_transaction():
            async with session.begin_nested():
                await AttributionEventRepository(session).append(
                    event_type=event_type,
                    actor=actor,
                    source_document_id=source_document_id,
                    module_id=module_id,
                    payload=payload,
                )
        else:
            async with session.begin():
                await AttributionEventRepository(session).append(
                    event_type=event_type,
                    actor=actor,
                    source_document_id=source_document_id,
                    module_id=module_id,
                    payload=payload,
                )
    except Exception:
        logger.exception("Failed to record attribution_event %s", event_type)
