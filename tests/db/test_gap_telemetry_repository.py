"""Gap telemetry claim ledger repository tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.repositories.gap_telemetry_repository import GapTelemetryRepository
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


@pytest.mark.asyncio
@requires_db
async def test_try_claim_event_is_idempotent(db_session: AsyncSession) -> None:
    repo = GapTelemetryRepository(db_session)
    event_id = uuid4()
    chw_id = 1001
    assert (
        await repo.try_claim_event(
            event_id=event_id,
            chw_id=chw_id,
            event_type="module_quiz_attempted",
            tenant_id=None,
        )
        is True
    )
    assert (
        await repo.try_claim_event(
            event_id=event_id,
            chw_id=chw_id,
            event_type="module_quiz_attempted",
            tenant_id=None,
        )
        is False
    )
    await db_session.commit()
