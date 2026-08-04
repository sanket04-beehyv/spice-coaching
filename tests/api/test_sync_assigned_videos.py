from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from mc_foundation.problem import AppError
from platform_service.api.admin_video_assignments import router as admin_video_assignments_router
from platform_service.api.sync import router as sync_router
from platform_service.config import get_settings
from platform_service.db.models.chw_video_assignment import CHWVideoAssignment
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.deps import get_db, get_object_storage_client
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]

# Hardcoded users from user_service: SK under PO 1708515793, other SK under different PO
SK_HOSNEYARA = 1313053891
SK_ANJALI = 1313054034
PO_ABDUS = 1708515793


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(db_session, "chw_video_assignment, source_page, source_document")
    yield


class _FakeStorage:
    bucket_name = "medtronics-storage"

    async def put_object_from_local_file(self, **_kwargs: object) -> None:
        return None

    async def presigned_get_url(self, **_kwargs: object) -> object:
        from mc_foundation.objectstore import PresignedObjectUrl

        return PresignedObjectUrl(
            url="https://minio.test/thumb.png",
            bucket_name=self.bucket_name,
            object_name="ingest/thumbnails/x.png",
            expires_seconds=3600,
        )


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> FastAPI:
    app_obj = FastAPI()

    @app_obj.middleware("http")
    async def mock_auth_middleware(request: Request, call_next):
        mock_user_id = request.headers.get("x-mock-user-id")
        mock_org_ids = request.headers.get("x-mock-org-ids")
        if mock_user_id:

            class MockSpiceUser:
                id = int(mock_user_id)
                tenant_id = None
                organization_ids = [int(x) for x in mock_org_ids.split(",")] if mock_org_ids else []

            request.state.spice_user = MockSpiceUser()
        return await call_next(request)

    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(admin_video_assignments_router)
    api_router.include_router(sync_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    app_obj.dependency_overrides[get_object_storage_client] = lambda: _FakeStorage()
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
    description: str | None = None,
    thumbnail_storage_path: str | None = None,
    status: str = "ingested",
) -> SourceDocument:
    doc = SourceDocument(
        title=title,
        description=description,
        source_type="video",
        primary_language="bn",
        content_domain="clinical",
        original_storage_path=f"medtronics-storage/ingest/{uuid4().hex}.mp4",
        original_filename=f"{title}.mp4",
        thumbnail_storage_path=thumbnail_storage_path,
        status=status,
    )
    session.add(doc)
    await session.flush()
    await session.commit()
    return doc


async def _assign(
    session: AsyncSession,
    *,
    video: SourceDocument,
    assignment_type: str,
    user_id: int | None = None,
    upazila: str | None = None,
    tenant_id: int | None = None,
) -> None:
    session.add(
        CHWVideoAssignment(
            source_document_id=video.id,
            assignment_type=assignment_type,
            user_id=user_id,
            upazila=upazila,
            tenant_id=tenant_id,
            assigned_by=1,
        )
    )
    await session.commit()


class TestSyncAssignedVideos:
    async def test_empty_assignments(self, client: AsyncClient) -> None:
        resp = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_HOSNEYARA},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["videos"] == []
        assert body["total_videos"] == 0
        assert body["total_pages"] == 0
        assert body["limit"] == 50
        assert body["offset"] == 0

    async def test_individual_sk_sees_own_video(self, client: AsyncClient, db_session: AsyncSession) -> None:
        video = await _seed_video(
            db_session,
            title="SK Video",
            description="Learn this",
            thumbnail_storage_path="medtronics-storage/ingest/thumbnails/a.png",
        )
        await _assign(
            db_session,
            video=video,
            assignment_type="individual",
            user_id=SK_HOSNEYARA,
        )

        resp = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_HOSNEYARA},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_videos"] == 1
        row = body["videos"][0]
        assert row["video_id"] == str(video.id)
        assert row["title"] == "SK Video"
        assert row["description"] == "Learn this"
        assert row["thumbnail_storage_path"] == "medtronics-storage/ingest/thumbnails/a.png"
        assert row["thumbnail_presigned_url"] == "https://minio.test/thumb.png"
        assert row["duration_ms"] is None
        assert row["assigned_at"]

        other = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_ANJALI},
        )
        assert other.json()["total_videos"] == 0

    async def test_po_sk_cascades_to_sk(self, client: AsyncClient, db_session: AsyncSession) -> None:
        video = await _seed_video(db_session, title="PO SK Video")
        await _assign(
            db_session,
            video=video,
            assignment_type="po_sk",
            user_id=PO_ABDUS,
        )

        for user_id in (SK_HOSNEYARA, PO_ABDUS):
            resp = await client.get(
                platform_path("/sync/assigned-videos"),
                params={"user_id": user_id},
            )
            assert resp.status_code == 200
            assert resp.json()["total_videos"] == 1
            assert resp.json()["videos"][0]["video_id"] == str(video.id)

        resp = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_ANJALI},
        )
        assert resp.json()["total_videos"] == 0

    async def test_geographical_assignment(self, client: AsyncClient, db_session: AsyncSession) -> None:
        video = await _seed_video(db_session, title="Geo Video")
        await _assign(
            db_session,
            video=video,
            assignment_type="geographical",
            upazila="Lalmonirhat Sadar",
        )

        resp = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_HOSNEYARA},
        )
        assert resp.status_code == 200
        assert resp.json()["total_videos"] == 1

    async def test_pagination(self, client: AsyncClient, db_session: AsyncSession) -> None:
        for i in range(3):
            video = await _seed_video(db_session, title=f"V{i}")
            await _assign(
                db_session,
                video=video,
                assignment_type="individual",
                user_id=SK_HOSNEYARA,
            )

        resp = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_HOSNEYARA, "limit": 2, "offset": 0},
        )
        body = resp.json()
        assert body["total_videos"] == 3
        assert body["total_pages"] == 2
        assert len(body["videos"]) == 2
        assert body["limit"] == 2
        assert body["offset"] == 0

        resp = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_HOSNEYARA, "limit": 2, "offset": 2},
        )
        body = resp.json()
        assert len(body["videos"]) == 1
        assert body["offset"] == 2

    async def test_duration_from_source_pages(self, client: AsyncClient, db_session: AsyncSession) -> None:
        video = await _seed_video(db_session, title="Timed Video")
        db_session.add(
            SourcePage(
                source_document_id=video.id,
                page_number=1,
                markdown_content="chunk",
                extraction_method="transcript",
                extraction_quality_score=1.0,
                start_ms=0,
                end_ms=45_000,
            )
        )
        db_session.add(
            SourcePage(
                source_document_id=video.id,
                page_number=2,
                markdown_content="chunk2",
                extraction_method="transcript",
                extraction_quality_score=1.0,
                start_ms=40_000,
                end_ms=90_000,
            )
        )
        await db_session.commit()
        await _assign(
            db_session,
            video=video,
            assignment_type="individual",
            user_id=SK_HOSNEYARA,
        )

        resp = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_HOSNEYARA},
        )
        assert resp.status_code == 200
        assert resp.json()["videos"][0]["duration_ms"] == 90_000

    async def test_excludes_non_video_source_documents(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        pdf = SourceDocument(
            title="not-video",
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
        await _assign(
            db_session,
            video=pdf,
            assignment_type="individual",
            user_id=SK_HOSNEYARA,
        )

        resp = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_HOSNEYARA},
        )
        assert resp.status_code == 200
        assert resp.json()["total_videos"] == 0

    async def test_auth_mismatch_forbidden_when_spice_enabled(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = await _seed_video(db_session, title="Auth Video")
        await _assign(
            db_session,
            video=video,
            assignment_type="individual",
            user_id=SK_HOSNEYARA,
        )

        settings = get_settings().model_copy(update={"spice_auth_enabled": True})
        with (
            patch("platform_service.auth.spice_identity.get_settings", return_value=settings),
            patch(
                "platform_service.auth.spice_identity.is_admin_principal",
                return_value=False,
            ),
            patch(
                "platform_service.auth.spice_identity.require_platform_tenant_for_spice_tenant",
                return_value=None,
            ),
        ):
            with pytest.raises(AppError) as exc_info:
                await client.get(
                    platform_path("/sync/assigned-videos"),
                    params={"user_id": SK_HOSNEYARA},
                    headers={"x-mock-user-id": str(SK_ANJALI)},
                )
        assert exc_info.value.status == 403

    async def test_group_assignment_via_organization_ids(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video = await _seed_video(db_session, title="Group Video")
        await _assign(
            db_session,
            video=video,
            assignment_type="group",
            tenant_id=5001,
        )

        resp = await client.get(
            platform_path("/sync/assigned-videos"),
            params={"user_id": SK_HOSNEYARA},
            headers={"x-mock-user-id": str(SK_HOSNEYARA), "x-mock-org-ids": "5001"},
        )
        assert resp.status_code == 200
        assert resp.json()["total_videos"] == 1
