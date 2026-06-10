"""Pipeline run exclusive-claim tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.run_state_service import RUN_RUNNING, RunStateService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def _seed_source(session: AsyncSession) -> SourceDocument:
    doc = SourceDocument(
        id=uuid4(),
        title="claim-test",
        source_type="pdf",
        authority_label="test",
        original_storage_path="bucket/key.pdf",
        status="uploaded",
    )
    session.add(doc)
    await session.flush()
    return doc


@pytest.mark.asyncio
@requires_db
async def test_try_claim_run_rejects_second_worker(db_session: AsyncSession) -> None:
    doc = await _seed_source(db_session)
    run = IngestionRun(source_document_id=doc.id, status=RUN_RUNNING)
    db_session.add(run)
    await db_session.flush()

    run_state = RunStateService(db_session)
    assert await run_state.try_claim_run(run.id, claim_token="worker-a") is True
    await db_session.commit()

    assert await run_state.try_claim_run(run.id, claim_token="worker-b") is False
