"""Admin ingestion-run API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.run_state_service import STAGE_CARD_DRAFT
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.conftest import _seed_module
from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


class TestIngestionRunEndpoints:
    async def _seed_run(
        self,
        session: AsyncSession,
        *,
        status: str = "succeeded",
        started_offset_seconds: int = 0,
        title: str | None = None,
        original_filename: str | None = None,
    ) -> IngestionRun:
        # Need a source_document for the FK.
        sd = SourceDocument(
            title=title or f"doc-{uuid4().hex[:6]}",
            source_type="pdf",
            primary_language="en",
            content_domain="clinical",
            assessment_mode="with_quiz",
            original_storage_path="/tmp/test.pdf",
            original_filename=original_filename,
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

    async def _add_card_draft_step(
        self,
        session: AsyncSession,
        run: IngestionRun,
        *,
        module_id: str | None,
    ) -> None:
        session.add(
            IngestionRunStep(
                ingestion_run_id=run.id,
                stage=STAGE_CARD_DRAFT,
                status="succeeded",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                output_summary_jsonb={"module_id": module_id},
            )
        )
        await session.commit()

    async def test_list_orders_by_started_desc(self, client: AsyncClient, db_session: AsyncSession) -> None:
        r1 = await self._seed_run(db_session, started_offset_seconds=0)
        r2 = await self._seed_run(db_session, started_offset_seconds=10)
        r3 = await self._seed_run(db_session, started_offset_seconds=20)

        resp = await client.get(platform_path("/admin/ingestion-runs"))
        body = resp.json()
        ids = [row["id"] for row in body["runs"]]
        # Newest-first.
        assert ids == [str(r3.id), str(r2.id), str(r1.id)]
        assert body["total_runs"] == 3

    async def test_list_filters_by_status(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await self._seed_run(db_session, status="succeeded", started_offset_seconds=0)
        await self._seed_run(db_session, status="failed", started_offset_seconds=10)

        resp = await client.get(platform_path("/admin/ingestion-runs?status=failed"))
        body = resp.json()
        statuses = {row["status"] for row in body["runs"]}
        assert statuses == {"failed"}
        assert body["total_runs"] == 1

    async def test_pagination_limit_offset(self, client: AsyncClient, db_session: AsyncSession) -> None:
        for i in range(5):
            await self._seed_run(db_session, started_offset_seconds=i)

        resp = await client.get(platform_path("/admin/ingestion-runs?limit=2&offset=0"))
        body = resp.json()
        assert len(body["runs"]) == 2
        assert body["total_runs"] == 5
        assert body["total_pages"] == 3
        assert body["limit"] == 2
        assert body["offset"] == 0

        resp = await client.get(platform_path("/admin/ingestion-runs?limit=2&offset=2"))
        body = resp.json()
        assert len(body["runs"]) == 2
        assert body["total_runs"] == 5
        assert body["offset"] == 2

        resp = await client.get(platform_path("/admin/ingestion-runs?limit=2&offset=4"))
        body = resp.json()
        assert len(body["runs"]) == 1
        assert body["total_runs"] == 5

    async def test_limit_validation_rejects_zero(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/ingestion-runs?limit=0"))
        assert resp.status_code == 422

    async def test_limit_validation_rejects_excessive(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/ingestion-runs?limit=500"))
        assert resp.status_code == 422

    async def test_list_includes_document_label_and_generated_counts(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        run = await self._seed_run(
            db_session,
            title="Guideline Title",
            original_filename="uhis-q1.pdf",
        )
        m1 = await _seed_module(
            db_session,
            module_json={
                "cards": [
                    {"title": {"bn": "1"}, "body": {"bn": "a"}},
                    {"title": {"bn": "2"}, "body": {"bn": "b"}},
                    {"title": {"bn": "3"}, "body": {"bn": "c"}},
                ]
            },
        )
        m2 = await _seed_module(
            db_session,
            module_json={
                "cards": [
                    {"title": {"bn": "4"}, "body": {"bn": "d"}},
                    {"title": {"bn": "5"}, "body": {"bn": "e"}},
                ]
            },
        )
        db_session.add(
            ModuleQuizQuestion(
                module_id=m1.id,
                question_order=1,
                question_family_id=uuid4(),
                question_version=1,
                question_localized={"bn": "Q1"},
                question_type="single_select",
                options_localized={"bn": ["a", "b", "c", "d"]},
                correct_indices=[0],
            )
        )
        db_session.add(
            ModuleQuizQuestion(
                module_id=m1.id,
                question_order=2,
                question_family_id=uuid4(),
                question_version=1,
                question_localized={"bn": "Q2"},
                question_type="single_select",
                options_localized={"bn": ["a", "b", "c", "d"]},
                correct_indices=[1],
            )
        )
        db_session.add(
            ModuleQuizQuestion(
                module_id=m2.id,
                question_order=1,
                question_family_id=uuid4(),
                question_version=1,
                question_localized={"bn": "Q3"},
                question_type="single_select",
                options_localized={"bn": ["a", "b", "c", "d"]},
                correct_indices=[0],
            )
        )
        await db_session.commit()

        await self._add_card_draft_step(db_session, run, module_id=str(m1.id))
        await self._add_card_draft_step(db_session, run, module_id=str(m2.id))
        # Duplicate module_id (retry) must not double-count.
        await self._add_card_draft_step(db_session, run, module_id=str(m1.id))
        await self._add_card_draft_step(db_session, run, module_id=None)

        resp = await client.get(platform_path("/admin/ingestion-runs"))
        assert resp.status_code == 200
        row = next(r for r in resp.json()["runs"] if r["id"] == str(run.id))
        assert row["document_label"] == "uhis-q1.pdf"
        assert row["generated_module_count"] == 2
        assert row["generated_card_count"] == 5
        assert row["generated_quiz_count"] == 3

    async def test_list_document_label_falls_back_to_title(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        run = await self._seed_run(
            db_session,
            title="Fallback Doc Title",
            original_filename=None,
        )
        resp = await client.get(platform_path("/admin/ingestion-runs"))
        row = next(r for r in resp.json()["runs"] if r["id"] == str(run.id))
        assert row["document_label"] == "Fallback Doc Title"
        assert row["generated_module_count"] == 0
        assert row["generated_card_count"] == 0
        assert row["generated_quiz_count"] == 0

    async def test_list_zeros_counts_for_non_succeeded(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        run = await self._seed_run(
            db_session,
            status="failed",
            original_filename="failed-run.pdf",
        )
        module = await _seed_module(
            db_session,
            module_json={
                "cards": [
                    {"title": {"bn": "1"}, "body": {"bn": "a"}},
                    {"title": {"bn": "2"}, "body": {"bn": "b"}},
                ]
            },
        )
        await self._add_card_draft_step(db_session, run, module_id=str(module.id))

        resp = await client.get(platform_path("/admin/ingestion-runs?status=failed"))
        row = next(r for r in resp.json()["runs"] if r["id"] == str(run.id))
        assert row["document_label"] == "failed-run.pdf"
        assert row["generated_module_count"] == 0
        assert row["generated_card_count"] == 0
        assert row["generated_quiz_count"] == 0

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

    async def test_detail_includes_document_label_and_generated_counts(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        run = await self._seed_run(
            db_session,
            title="Detail Doc",
            original_filename="detail.pdf",
        )
        module = await _seed_module(
            db_session,
            module_json={
                "cards": [
                    {"title": {"bn": "1"}, "body": {"bn": "a"}},
                    {"title": {"bn": "2"}, "body": {"bn": "b"}},
                    {"title": {"bn": "3"}, "body": {"bn": "c"}},
                    {"title": {"bn": "4"}, "body": {"bn": "d"}},
                ]
            },
        )
        db_session.add(
            ModuleQuizQuestion(
                module_id=module.id,
                question_order=1,
                question_family_id=uuid4(),
                question_version=1,
                question_localized={"bn": "Q"},
                question_type="single_select",
                options_localized={"bn": ["a", "b", "c", "d"]},
                correct_indices=[0],
            )
        )
        await db_session.commit()
        await self._add_card_draft_step(db_session, run, module_id=str(module.id))

        resp = await client.get(platform_path(f"/admin/ingestion-runs/{run.id}"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["document_label"] == "detail.pdf"
        assert data["generated_module_count"] == 1
        assert data["generated_card_count"] == 4
        assert data["generated_quiz_count"] == 1

    async def test_detail_404_for_unknown(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path(f"/admin/ingestion-runs/{uuid4()}"))
        assert resp.status_code == 404
