"""Unit tests for module_identify parent vs chunk step helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.run_state.steps import is_module_identify_chunk_step
from platform_service.services.run_state_service import (
    RUN_RUNNING,
    STAGE_MODULE_IDENTIFY,
    STEP_FAILED,
    STEP_SUCCEEDED,
    RunStateService,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(db_session, "ingestion_run_step, ingestion_run, source_document")
    yield


async def _seed_run(session: AsyncSession) -> IngestionRun:
    doc = SourceDocument(
        title="id-helpers",
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        original_storage_path="bucket/x.pdf",
        status="ingesting",
    )
    session.add(doc)
    await session.flush()
    run = IngestionRun(source_document_id=doc.id, status=RUN_RUNNING)
    session.add(run)
    await session.commit()
    return run


class TestModuleIdentifyStepHelpers:
    async def test_parent_vs_chunk_and_fully_succeeded(self, db_session: AsyncSession) -> None:
        run = await _seed_run(db_session)
        parent = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"source_document_ids": [str(uuid4())]},
        )
        chunk_ok = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"chunk_id": "chunk-1"},
        )
        chunk_bad = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_FAILED,
            started_at=datetime.now(UTC),
            input_summary_jsonb={"chunk_id": "chunk-2"},
        )
        db_session.add_all([parent, chunk_ok, chunk_bad])
        await db_session.commit()

        rs = RunStateService(db_session)
        found_parent = await rs.find_module_identify_parent(run.id)
        assert found_parent is not None
        assert found_parent.id == parent.id
        assert not is_module_identify_chunk_step(found_parent)

        chunks = await rs.list_module_identify_chunk_steps(run.id)
        assert {(s.input_summary_jsonb or {}).get("chunk_id") for s in chunks} == {
            "chunk-1",
            "chunk-2",
        }
        assert await rs.is_module_identify_fully_succeeded(run.id) is False

        chunk_bad.status = STEP_SUCCEEDED
        await db_session.commit()
        assert await rs.is_module_identify_fully_succeeded(run.id) is True

    async def test_legacy_parent_only_is_fully_succeeded(self, db_session: AsyncSession) -> None:
        run = await _seed_run(db_session)
        parent = IngestionRunStep(
            ingestion_run_id=run.id,
            stage=STAGE_MODULE_IDENTIFY,
            status=STEP_SUCCEEDED,
            started_at=datetime.now(UTC),
        )
        db_session.add(parent)
        await db_session.commit()
        rs = RunStateService(db_session)
        assert await rs.is_module_identify_fully_succeeded(run.id) is True
