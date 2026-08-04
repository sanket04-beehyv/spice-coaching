"""Daily worker: infer module-creation suggestions from unattributed demand."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from platform_service.db.base import SessionLocal
from platform_service.services.module_creation_suggestion_service import (
    ModuleCreationSuggestionService,
)

logger = logging.getLogger(__name__)


async def refresh_module_creation_suggestions_job() -> dict[str, int]:
    """Refresh suggestions for every scope for the previous UTC calendar day.

    LLM / parse failures propagate so Celery can retry. Prior rows for a day are
    only replaced after a successful classify, so failed runs leave data intact.
    """
    suggestion_date = datetime.now(UTC).date() - timedelta(days=1)
    scopes_updated = 0
    suggestions_written = 0
    async with SessionLocal() as session:
        service = ModuleCreationSuggestionService(session)
        scopes = await service.list_scopes()
        for tenant_id in scopes:
            count = await service.refresh_for_day(
                tenant_id=tenant_id,
                suggestion_date=suggestion_date,
            )
            scopes_updated += 1
            suggestions_written += count

    logger.info(
        "Module creation suggestion refresh complete: scopes=%d suggestions=%d date=%s",
        scopes_updated,
        suggestions_written,
        suggestion_date,
    )
    return {
        "scopes_updated": scopes_updated,
        "suggestions_written": suggestions_written,
    }
