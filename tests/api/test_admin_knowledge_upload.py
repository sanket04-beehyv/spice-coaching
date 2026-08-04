"""Admin knowledge PDF upload API tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pymupdf  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from mc_foundation.objectstore import ObjectNotFoundError, StoredObject
from mc_foundation.problem import register_problem_handlers
from platform_service.api.knowledge import router as knowledge_router
from platform_service.config import get_settings
from platform_service.db.models.source_document import SourceDocument
from platform_service.deps import get_db, get_object_storage_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]

_BUCKET = "medtronics-storage"


def _pdf_bytes(pages: int = 1) -> bytes:
    with pymupdf.open() as doc:
        for index in range(pages):
            page = doc.new_page()
            page.insert_text((72, 72), f"Page {index + 1}")
        return doc.tobytes()


@pytest_asyncio.fixture(autouse=True)
async def _wipe_tables(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(db_session, "attribution_event, file_upload, source_document")
    yield


@pytest_asyncio.fixture
async def storage_mock() -> MagicMock:
    storage = MagicMock()
    storage.bucket_name = _BUCKET
    storage.allowed_prefixes = frozenset({"uploads", "source-documents", "media", "ingest"})

    async def _put(*, object_name: str, local_path, content_type: str, metadata) -> StoredObject:
        size = Path(local_path).stat().st_size
        return StoredObject(
            bucket_name=_BUCKET,
            object_name=object_name,
            storage_path=f"{_BUCKET}/{object_name}",
            content_type=content_type,
            size_bytes=size,
        )

    storage.put_object_from_local_file = AsyncMock(side_effect=_put)
    storage.stat_object = AsyncMock()
    return storage


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
    storage_mock: MagicMock,
) -> AsyncIterator[AsyncClient]:
    app_obj = FastAPI()
    register_problem_handlers(
        app_obj,
        validation_error_type=RequestValidationError,
        http_exception_type=HTTPException,
    )
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(knowledge_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    app_obj.dependency_overrides[get_object_storage_client] = lambda: storage_mock

    transport = ASGITransport(app=app_obj)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app_obj.dependency_overrides.clear()


async def _upload(
    client: AsyncClient,
    *,
    content: bytes,
    filename: str = "manual.pdf",
    data: dict[str, str] | None = None,
) -> Any:
    return await client.post(
        platform_path("/admin/knowledge/upload"),
        data=data or {},
        files={"file": (filename, BytesIO(content), "application/pdf")},
    )


class TestKnowledgeUpload:
    async def test_whole_file_upload_creates_visible_source_document(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        storage_mock: MagicMock,
    ) -> None:
        resp = await _upload(client, content=_pdf_bytes(2), data={"title": "RMNCH Manual"})
        assert resp.status_code == 201
        body = resp.json()
        assert len(body["sources"]) == 1
        source = body["sources"][0]
        assert source["title"] == "RMNCH Manual"
        assert source["start_page"] is None
        assert source["end_page"] is None
        assert source["stored_path"].startswith(f"{_BUCKET}/source-documents/knowledge/")

        doc_id = UUID(source["source_document_id"])
        doc = (
            await db_session.execute(select(SourceDocument).where(SourceDocument.id == doc_id))
        ).scalar_one()
        assert doc.sync_published_visible is True
        assert doc.status == "uploaded"
        assert doc.source_type == "pdf"
        storage_mock.put_object_from_local_file.assert_awaited()

    async def test_whole_file_title_falls_back_to_basename(
        self,
        client: AsyncClient,
    ) -> None:
        resp = await _upload(client, content=_pdf_bytes(1), filename="referral-guide.pdf")
        assert resp.status_code == 201
        assert resp.json()["sources"][0]["title"] == "referral-guide"

    async def test_splits_create_one_document_each(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        storage_mock: MagicMock,
    ) -> None:
        splits = [
            {"start_page": 1, "end_page": 2, "title": "Part A"},
            {"start_page": 3, "end_page": 3, "title": "Part B"},
        ]
        resp = await _upload(
            client,
            content=_pdf_bytes(3),
            data={"splits": json.dumps(splits), "title": "ignored-whole-title"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert len(body["sources"]) == 2
        assert {s["title"] for s in body["sources"]} == {"Part A", "Part B"}
        assert body["sources"][0]["start_page"] == 1
        assert body["sources"][0]["end_page"] == 2
        assert body["sources"][1]["start_page"] == 3
        assert body["sources"][1]["end_page"] == 3
        assert storage_mock.put_object_from_local_file.await_count == 2

        ids = [UUID(s["source_document_id"]) for s in body["sources"]]
        docs = (
            (await db_session.execute(select(SourceDocument).where(SourceDocument.id.in_(ids))))
            .scalars()
            .all()
        )
        assert len(docs) == 2
        assert all(doc.sync_published_visible for doc in docs)
        family_ids = {doc.source_document_family_id for doc in docs}
        assert len(family_ids) == 2

    async def test_invalid_page_range_returns_400(self, client: AsyncClient) -> None:
        splits = [{"start_page": 1, "end_page": 5, "title": "Too far"}]
        resp = await _upload(
            client,
            content=_pdf_bytes(2),
            data={"splits": json.dumps(splits)},
        )
        assert resp.status_code == 400
        assert "page count" in resp.json()["detail"].lower() or "exceeds" in resp.text.lower()

    async def test_missing_thumbnail_returns_400(
        self,
        client: AsyncClient,
        storage_mock: MagicMock,
    ) -> None:
        storage_mock.stat_object = AsyncMock(side_effect=ObjectNotFoundError("missing"))
        resp = await _upload(
            client,
            content=_pdf_bytes(1),
            data={
                "title": "With thumb",
                "thumbnail_storage_path": f"{_BUCKET}/uploads/missing.png",
            },
        )
        assert resp.status_code == 400
        assert "not found" in resp.text.lower()

    async def test_rejects_non_pdf(self, client: AsyncClient) -> None:
        resp = await client.post(
            platform_path("/admin/knowledge/upload"),
            files={"file": ("notes.txt", BytesIO(b"hello"), "text/plain")},
        )
        assert resp.status_code == 400
