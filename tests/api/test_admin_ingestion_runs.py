"""Admin ingestion-run API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.source_document import SourceDocument
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


class TestIngestionRunEndpoints:
    async def _seed_run(
        self,
        session: AsyncSession,
        *,
        status: str = "succeeded",
        started_offset_seconds: int = 0,
    ) -> IngestionRun:
        # Need a source_document for the FK.
        sd = SourceDocument(
            title=f"doc-{uuid4().hex[:6]}",
            source_type="pdf",
            primary_language="en",
            content_domain="clinical",
            assessment_mode="with_quiz",
            authority_label="BRAC",
            original_storage_path="/tmp/test.pdf",
        )
        session.add(sd)
        await session.flush()
        run = IngestionRun(
            source_document_id=sd.id,
            status=status,
            started_at=datetime.now(UTC) + timedelta(seconds=started_offset_seconds),
            completed_at=datetime.now(UTC) if status != "running" else None,
        )
        session.add(run)
        await session.flush()
        await session.commit()
        return run

    async def test_list_orders_by_started_desc(self, client: AsyncClient, db_session: AsyncSession) -> None:
        r1 = await self._seed_run(db_session, started_offset_seconds=0)
        r2 = await self._seed_run(db_session, started_offset_seconds=10)
        r3 = await self._seed_run(db_session, started_offset_seconds=20)

        resp = await client.get(platform_path("/admin/ingestion-runs"))
        ids = [row["id"] for row in resp.json()]
        # Newest-first.
        assert ids == [str(r3.id), str(r2.id), str(r1.id)]

    async def test_list_filters_by_status(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await self._seed_run(db_session, status="succeeded", started_offset_seconds=0)
        await self._seed_run(db_session, status="failed", started_offset_seconds=10)

        resp = await client.get(platform_path("/admin/ingestion-runs?status=failed"))
        statuses = {row["status"] for row in resp.json()}
        assert statuses == {"failed"}

    async def test_detail_includes_steps_in_order(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        run = await self._seed_run(db_session)
        # Add three steps with increasing started_at.
        for i, stage in enumerate(("extract", "module_identify", "card_draft")):
            db_session.add(
                IngestionRunStep(
                    ingestion_run_id=run.id,
                    stage=stage,
                    status="succeeded",
                    started_at=datetime.now(UTC) + timedelta(seconds=i),
                    completed_at=datetime.now(UTC) + timedelta(seconds=i + 1),
                )
            )
        await db_session.commit()

        resp = await client.get(platform_path(f"/admin/ingestion-runs/{run.id}"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(run.id)
        stages = [s["stage"] for s in data["steps"]]
        assert stages == ["extract", "module_identify", "card_draft"]

    async def test_detail_404_for_unknown(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path(f"/admin/ingestion-runs/{uuid4()}"))
        assert resp.status_code == 404
