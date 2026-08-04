"""Admin knowledge soft-delete (retire) API tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from mc_foundation.problem import register_problem_handlers
from platform_service.api.knowledge import router as knowledge_router
from platform_service.config import get_settings
from platform_service.db.models.source_document import SourceDocument
from platform_service.deps import get_db, get_object_storage_client
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]

_BUCKET = "medtronics-storage"


@pytest_asyncio.fixture(autouse=True)
async def _wipe_tables(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(db_session, "source_document")
    yield


@pytest_asyncio.fixture
async def storage_mock() -> MagicMock:
    storage = MagicMock()
    storage.bucket_name = _BUCKET
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


async def _seed_doc(
    session: AsyncSession,
    *,
    sync_published_visible: bool,
    status: str = "uploaded",
) -> SourceDocument:
    doc = SourceDocument(
        title="Knowledge doc",
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        original_storage_path=f"{_BUCKET}/source-documents/knowledge/{uuid4()}.pdf",
        original_filename="manual.pdf",
        sync_published_visible=sync_published_visible,
        status=status,
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return doc


async def _retire(client: AsyncClient, source_document_id: Any) -> Any:
    return await client.delete(platform_path(f"/admin/knowledge/{source_document_id}"))


class TestKnowledgeRetire:
    async def test_retire_knowledge_document(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        doc = await _seed_doc(db_session, sync_published_visible=True)

        resp = await _retire(client, doc.id)
        assert resp.status_code == 204
        assert resp.content == b""

        await db_session.refresh(doc)
        assert doc.status == "retired"
        assert doc.sync_published_visible is True

    async def test_retire_is_idempotent(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        doc = await _seed_doc(db_session, sync_published_visible=True, status="retired")

        resp = await _retire(client, doc.id)
        assert resp.status_code == 204

        await db_session.refresh(doc)
        assert doc.status == "retired"
        assert doc.sync_published_visible is True

    async def test_retire_missing_returns_404(self, client: AsyncClient) -> None:
        resp = await _retire(client, uuid4())
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "source_not_found"

    async def test_retire_non_knowledge_returns_403(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        doc = await _seed_doc(db_session, sync_published_visible=False, status="uploaded")

        resp = await _retire(client, doc.id)
        assert resp.status_code == 403
        body = resp.json()
        assert body["code"] == "forbidden"

        await db_session.refresh(doc)
        assert doc.status == "uploaded"
        assert doc.sync_published_visible is False
