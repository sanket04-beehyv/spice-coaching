"""Admin ingest/fusion endpoints enqueue Celery tasks (not FastAPI BackgroundTasks)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.api.admin_ingest import router as admin_ingest_router
from platform_service.config import get_settings
from platform_service.db.models.source_document import SourceDocument
from platform_service.deps import get_db, get_object_storage_client
from platform_service.services.object_storage import StoredObject
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

_BUCKET = "medtronics-storage"
_DUPLICATE_PDF_BYTES = b"%PDF-1.4 minimal"
_DUPLICATE_PDF_SHA256 = hashlib.sha256(_DUPLICATE_PDF_BYTES).hexdigest()


@pytest_asyncio.fixture(autouse=True)
async def _wipe_ingest_tables(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text("TRUNCATE attribution_event, file_upload, source_document RESTART IDENTITY CASCADE")
    )
    await db_session.commit()


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(admin_ingest_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    app_obj.dependency_overrides[get_object_storage_client] = _mock_ingest_storage
    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _mock_ingest_storage() -> MagicMock:
    storage = MagicMock()

    async def _put(*, object_name: str, local_path, content_type: str, metadata) -> StoredObject:
        size = local_path.stat().st_size
        return StoredObject(
            bucket_name=_BUCKET,
            object_name=object_name,
            storage_path=f"{_BUCKET}/{object_name}",
            content_type=content_type,
            size_bytes=size,
        )

    storage.put_object_from_local_file = AsyncMock(side_effect=_put)
    return storage


async def _seed_ingested_source(
    db_session: AsyncSession,
    *,
    content_sha256: str = _DUPLICATE_PDF_SHA256,
    title: str = "Existing Guide",
) -> SourceDocument:
    doc = SourceDocument(
        title=title,
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        assessment_mode="with_quiz",
        authority_label="BRAC",
        original_storage_path=f"{_BUCKET}/ingest/existing.pdf",
        content_sha256=content_sha256,
        original_filename="guide.pdf",
        status="ingested",
    )
    db_session.add(doc)
    await db_session.flush()
    return doc


class TestIngestEnqueuesCelery:
    async def test_start_ingest_enqueues_batch_task(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from platform_service import celery_tasks

        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.run_ingest_batch_task, "delay", delay_mock)

        resp = await client.post(
            platform_path("/admin/ingest"),
            data={"primary_language": "bn", "skip_merge": "true"},
            files=[("files", ("guide.pdf", BytesIO(b"%PDF-1.4 minimal"), "application/pdf"))],
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "batch_queued"
        assert body["skip_merge"] is True
        assert len(body["sources"]) == 1

        delay_mock.assert_called_once()
        payload = delay_mock.call_args[0][0]
        assert payload["fuse_sources"] is False
        assert len(payload["jobs"]) == 1
        job = payload["jobs"][0]
        assert job["source_document_id"] == body["sources"][0]["source_document_id"]
        assert job["source_type"] == "pdf"
        assert job["primary_language"] == "bn"
        assert job["skip_merge"] is True
        assert job["source_path"].startswith(f"{_BUCKET}/ingest/")

    async def test_start_ingest_with_fusion_passes_flag(
        self,
        client: AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from platform_service import celery_tasks

        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.run_ingest_batch_task, "delay", delay_mock)

        resp = await client.post(
            platform_path("/admin/ingest"),
            data={"fuse_sources": "true", "primary_language": "en"},
            files=[
                ("files", ("a.pdf", BytesIO(b"%PDF-1"), "application/pdf")),
                ("files", ("b.pdf", BytesIO(b"%PDF-2"), "application/pdf")),
            ],
        )
        assert resp.status_code == 202
        payload = delay_mock.call_args[0][0]
        assert payload["fuse_sources"] is True
        assert len(payload["jobs"]) == 2


class TestIngestDuplicateOverride:
    async def test_batch_duplicate_without_override_returns_409(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from platform_service import celery_tasks

        existing = await _seed_ingested_source(db_session)
        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.run_ingest_batch_task, "delay", delay_mock)

        resp = await client.post(
            platform_path("/admin/ingest"),
            data={"primary_language": "bn"},
            files=[("files", ("guide.pdf", BytesIO(_DUPLICATE_PDF_BYTES), "application/pdf"))],
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["detail"]["code"] == "duplicate_content"
        assert len(body["detail"]["conflicts"]) == 1
        conflict = body["detail"]["conflicts"][0]
        assert conflict["content_sha256"] == _DUPLICATE_PDF_SHA256
        assert conflict["existing_source_documents"][0]["source_document_id"] == str(existing.id)
        delay_mock.assert_not_called()

    async def test_batch_duplicate_with_override_re_ingests(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from platform_service import celery_tasks

        await _seed_ingested_source(db_session)
        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.run_ingest_batch_task, "delay", delay_mock)

        resp = await client.post(
            platform_path("/admin/ingest"),
            data={"primary_language": "bn", "override_duplicates": "[true]"},
            files=[("files", ("guide.pdf", BytesIO(_DUPLICATE_PDF_BYTES), "application/pdf"))],
        )
        assert resp.status_code == 202
        assert len(resp.json()["sources"]) == 1
        delay_mock.assert_called_once()

    async def test_batch_partial_success_returns_skipped_duplicates(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from platform_service import celery_tasks

        await _seed_ingested_source(db_session)
        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.run_ingest_batch_task, "delay", delay_mock)

        resp = await client.post(
            platform_path("/admin/ingest"),
            data={"primary_language": "bn", "override_duplicates": "[false, false]"},
            files=[
                ("files", ("new.pdf", BytesIO(b"%PDF-new"), "application/pdf")),
                ("files", ("guide.pdf", BytesIO(_DUPLICATE_PDF_BYTES), "application/pdf")),
            ],
        )
        assert resp.status_code == 202
        body = resp.json()
        assert len(body["sources"]) == 1
        assert body["sources"][0]["source_type"] == "pdf"
        assert len(body["skipped_duplicates"]) == 1
        assert body["skipped_duplicates"][0]["filename"] == "guide.pdf"
        delay_mock.assert_called_once()

    async def test_failed_source_does_not_block_reupload(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from platform_service import celery_tasks

        failed = SourceDocument(
            title="Failed Guide",
            source_type="pdf",
            primary_language="bn",
            content_domain="clinical",
            assessment_mode="with_quiz",
            authority_label="BRAC",
            original_storage_path=f"{_BUCKET}/ingest/failed.pdf",
            content_sha256=_DUPLICATE_PDF_SHA256,
            original_filename="guide.pdf",
            status="failed",
        )
        db_session.add(failed)
        await db_session.flush()

        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.run_ingest_batch_task, "delay", delay_mock)

        resp = await client.post(
            platform_path("/admin/ingest"),
            data={"primary_language": "bn"},
            files=[("files", ("guide.pdf", BytesIO(_DUPLICATE_PDF_BYTES), "application/pdf"))],
        )
        assert resp.status_code == 202
        assert len(resp.json()["sources"]) == 1
        delay_mock.assert_called_once()

    async def test_fuse_sources_rejects_when_only_one_file_ingested(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from platform_service import celery_tasks

        await _seed_ingested_source(db_session)
        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.run_ingest_batch_task, "delay", delay_mock)

        resp = await client.post(
            platform_path("/admin/ingest"),
            data={"fuse_sources": "true", "primary_language": "bn"},
            files=[
                ("files", ("new.pdf", BytesIO(b"%PDF-new"), "application/pdf")),
                ("files", ("guide.pdf", BytesIO(_DUPLICATE_PDF_BYTES), "application/pdf")),
            ],
        )
        assert resp.status_code == 400
        assert "2 successfully ingested" in resp.json()["detail"]
        delay_mock.assert_not_called()

    async def test_stream_duplicate_without_override_returns_409(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from platform_service import celery_tasks

        existing = await _seed_ingested_source(db_session)
        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.generate_source_thumbnail_task, "delay", delay_mock)

        resp = await client.post(
            platform_path("/admin/ingest/stream"),
            data={"title": "Guide", "primary_language": "bn"},
            files=[("file", ("guide.pdf", BytesIO(_DUPLICATE_PDF_BYTES), "application/pdf"))],
        )
        assert resp.status_code == 409
        conflict = resp.json()["detail"]["conflicts"][0]
        assert conflict["existing_source_documents"][0]["source_document_id"] == str(existing.id)
        delay_mock.assert_not_called()


class TestFusionEnqueuesCelery:
    async def test_start_fusion_enqueues_task(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from platform_service import celery_tasks

        doc_a = SourceDocument(
            title="A",
            source_type="pdf",
            primary_language="bn",
            content_domain="clinical",
            assessment_mode="with_quiz",
            authority_label="BRAC",
            original_storage_path=f"{_BUCKET}/ingest/a.pdf",
        )
        doc_b = SourceDocument(
            title="B",
            source_type="pdf",
            primary_language="bn",
            content_domain="clinical",
            assessment_mode="with_quiz",
            authority_label="BRAC",
            original_storage_path=f"{_BUCKET}/ingest/b.pdf",
        )
        db_session.add_all([doc_a, doc_b])
        await db_session.flush()

        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.run_cross_source_fusion_task, "delay", delay_mock)

        resp = await client.post(
            platform_path("/admin/fusion"),
            json={"source_document_ids": [str(doc_a.id), str(doc_b.id)]},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "fusion_queued"

        delay_mock.assert_called_once_with({"source_document_ids": [str(doc_a.id), str(doc_b.id)]})

    async def test_start_fusion_rejects_single_source(self, client: AsyncClient) -> None:
        resp = await client.post(
            platform_path("/admin/fusion"),
            json={"source_document_ids": [str(uuid4())]},
        )
        assert resp.status_code == 422
