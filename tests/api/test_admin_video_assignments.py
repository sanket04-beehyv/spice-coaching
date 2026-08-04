from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from platform_service.api.admin_video_assignments import router as admin_video_assignments_router
from platform_service.config import get_settings
from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.db.models.source_document import SourceDocument
from platform_service.deps import get_db
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(
        db_session, "chw_video_assignment, ingestion_run_step, ingestion_run, source_document"
    )
    yield


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> FastAPI:
    app_obj = FastAPI()

    @app_obj.middleware("http")
    async def mock_auth_middleware(request: Request, call_next):
        mock_user_id = request.headers.get("x-mock-user-id")
        if mock_user_id:

            class MockSpiceUser:
                id = int(mock_user_id)

            request.state.spice_user = MockSpiceUser()
        return await call_next(request)

    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(admin_video_assignments_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_video(
    session: AsyncSession,
    *,
    title: str = "training-clip",
    status: str = "ingested",
) -> SourceDocument:
    doc = SourceDocument(
        title=title,
        source_type="video",
        primary_language="bn",
        content_domain="clinical",
        original_storage_path=f"medtronics-storage/ingest/{uuid4().hex}.mp4",
        original_filename=f"{title}.mp4",
        status=status,
    )
    session.add(doc)
    await session.flush()
    await session.commit()
    return doc


class TestAdminVideoAssignments:
    async def test_crud_assignment_workflow(self, client: AsyncClient, db_session: AsyncSession) -> None:
        video_1 = await _seed_video(db_session, title="Video One")
        video_2 = await _seed_video(db_session, title="Video Two")

        resp = await client.post(
            platform_path("/admin/video-assignments"),
            json={
                "source_document_id": str(video_1.id),
                "assignment_type": "individual",
                "user_ids": [1313053891, 1313053895],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["assigned_count"] == 2
        assert len(data["assignment_ids"]) == 2

        resp = await client.post(
            platform_path("/admin/video-assignments"),
            json={
                "source_document_id": str(video_1.id),
                "assignment_type": "individual",
                "user_ids": [1313053891, 1313053892],
            },
        )
        assert resp.status_code == 201
        assert resp.json()["assigned_count"] == 2

        resp = await client.post(
            platform_path("/admin/video-assignments"),
            json={
                "source_document_id": str(video_2.id),
                "assignment_type": "group",
                "tenant_ids": [5001],
            },
        )
        assert resp.status_code == 201

        resp = await client.get(platform_path("/admin/video-assignments"))
        assert resp.status_code == 200
        assignments = resp.json()
        assert len(assignments) == 4

        a1 = [a for a in assignments if a["source_document_id"] == str(video_1.id)]
        assert len(a1) == 3
        assert a1[0]["video_title"] == "Video One"

        assignment_id_to_revoke = assignments[0]["id"]
        resp = await client.delete(platform_path(f"/admin/video-assignments/{assignment_id_to_revoke}"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"

        resp = await client.get(platform_path("/admin/video-assignments"))
        assert len(resp.json()) == 3

    async def test_po_sk_and_geographical(self, client: AsyncClient, db_session: AsyncSession) -> None:
        video = await _seed_video(db_session, title="Geo Video")

        resp = await client.post(
            platform_path("/admin/video-assignments"),
            json={
                "source_document_id": str(video.id),
                "assignment_type": "po_sk",
                "user_ids": [1708515793],
            },
        )
        assert resp.status_code == 201

        resp = await client.post(
            platform_path("/admin/video-assignments"),
            json={
                "source_document_id": str(video.id),
                "assignment_type": "geographical",
                "upazilas": ["Lalmonirhat Sadar"],
            },
        )
        assert resp.status_code == 201

        resp = await client.get(
            platform_path("/admin/video-assignments"),
            params={"source_document_id": str(video.id)},
        )
        assert resp.status_code == 200
        types = {a["assignment_type"] for a in resp.json()}
        assert types == {"po_sk", "geographical"}

    async def test_rejects_non_video_source_document(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        pdf = SourceDocument(
            title="not-a-video",
            source_type="pdf",
            primary_language="bn",
            content_domain="clinical",
            original_storage_path=f"medtronics-storage/ingest/{uuid4().hex}.pdf",
            original_filename="doc.pdf",
            status="ingested",
        )
        db_session.add(pdf)
        await db_session.flush()
        await db_session.commit()

        resp = await client.post(
            platform_path("/admin/video-assignments"),
            json={
                "source_document_id": str(pdf.id),
                "assignment_type": "individual",
                "user_ids": [1313053891],
            },
        )
        assert resp.status_code == 404

    async def test_assign_while_ingesting(self, client: AsyncClient, db_session: AsyncSession) -> None:
        video = await _seed_video(db_session, title="In Flight", status="ingesting")

        resp = await client.post(
            platform_path("/admin/video-assignments"),
            json={
                "source_document_id": str(video.id),
                "assignment_type": "individual",
                "user_ids": [1313053891],
            },
        )
        assert resp.status_code == 201

        await db_session.refresh(video)
        assert video.status == "ingesting"

    async def test_assign_does_not_create_ingestion_run(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = await _seed_video(db_session, title="No Reingest")
        before = (await db_session.execute(select(func.count()).select_from(IngestionRun))).scalar_one()

        resp = await client.post(
            platform_path("/admin/video-assignments"),
            json={
                "source_document_id": str(video.id),
                "assignment_type": "individual",
                "user_ids": [1313053891],
            },
        )
        assert resp.status_code == 201

        after = (await db_session.execute(select(func.count()).select_from(IngestionRun))).scalar_one()
        assert after == before

    async def test_po_sk_rejects_non_po(self, client: AsyncClient, db_session: AsyncSession) -> None:
        video = await _seed_video(db_session, title="PO Only")

        resp = await client.post(
            platform_path("/admin/video-assignments"),
            json={
                "source_document_id": str(video.id),
                "assignment_type": "po_sk",
                "user_ids": [1313053891],  # SK
            },
        )
        assert resp.status_code == 400
