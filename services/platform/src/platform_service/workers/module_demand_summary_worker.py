"""Daily worker: precompute the admin module-demand summary snapshot.

Computing the summary on every admin request fans out to ClickHouse and
ai-runtime. This job builds one snapshot per tenant scope (global + each active
tenant) so ``GET /admin/module-demand/summary`` can read it back cheaply.
"""

from __future__ import annotations

import logging

from platform_service.db.base import SessionLocal
from platform_service.services.module_demand_service import ModuleDemandService

logger = logging.getLogger(__name__)


async def refresh_module_demand_summaries_job() -> dict[str, int]:
    """Refresh the cached module-demand summary for every scope."""
    scopes_updated = 0
    async with SessionLocal() as session:
        service = ModuleDemandService(session)
        scopes = await service.list_summary_scopes()
        for tenant_id in scopes:
            try:
                await service.refresh_summary(tenant_id=tenant_id)
                scopes_updated += 1
            except Exception:
                logger.exception("Module demand summary refresh failed for tenant %s", tenant_id)
                await session.rollback()
                continue

    logger.info("Module demand summary refresh complete: scopes=%d", scopes_updated)
    return {"scopes_updated": scopes_updated}
