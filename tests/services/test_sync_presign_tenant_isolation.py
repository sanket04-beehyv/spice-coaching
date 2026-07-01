"""Tenant isolation for device sync presign endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from platform_service.config import Settings
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.sync_service import SyncService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

_BUCKET = "medtronics-storage"
_TENANT_A = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B = UUID("22222222-2222-2222-2222-222222222222")
_STORAGE_PATH = f"{_BUCKET}/source-documents/tenant-test.pdf"
_THUMB_PATH = f"{_BUCKET}/ingest/thumbnails/tenant-test.png"


@pytest_asyncio.fixture(autouse=True)
async def _rollback(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()


def _mock_storage() -> MagicMock:
    storage = MagicMock()
    storage.presigned_get_url = AsyncMock(
        return_value=type("Url", (), {"url": "https://minio.example/obj"})()
    )
    return storage


async def _seed_source_document(session: AsyncSession) -> SourceDocument:
    doc = SourceDocument(
        title="tenant isolation doc",
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        assessment_mode="read_only",
        authority_label="test",
        original_storage_path=_STORAGE_PATH,
        thumbnail_storage_path=_THUMB_PATH,
        status="ingested",
    )
    session.add(doc)
    await session.flush()
    return doc


async def _seed_module(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    source_document_ids: list[UUID] | None = None,
    thumbnail_storage_path: str | None = _THUMB_PATH,
) -> Module:
    family = ModuleFamily(module_code=f"tenant-iso-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": "tenant module"},
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        tenant_id=tenant_id,
        source_document_ids=source_document_ids,
        thumbnail_storage_path=thumbnail_storage_path,
    )
    session.add(module)
    await session.flush()
    await session.commit()
    return module


@pytest.mark.asyncio
@requires_db
async def test_presign_source_document_rejects_other_tenant(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session)
    await _seed_module(db_session, tenant_id=_TENANT_A, source_document_ids=[doc.id])
    storage = _mock_storage()
    settings = Settings(minio_bucket_name=_BUCKET)

    resp = await SyncService(db_session).get_source_document_presigned_urls(
        source_document_ids=[doc.id],
        storage=storage,
        settings=settings,
        tenant_id=_TENANT_B,
    )

    assert resp.urls == []
    assert resp.missing_ids == [doc.id]
    storage.presigned_get_url.assert_not_awaited()


@pytest.mark.asyncio
@requires_db
async def test_presign_source_document_allows_own_tenant(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session)
    await _seed_module(db_session, tenant_id=_TENANT_A, source_document_ids=[doc.id])
    storage = _mock_storage()
    settings = Settings(minio_bucket_name=_BUCKET)

    resp = await SyncService(db_session).get_source_document_presigned_urls(
        source_document_ids=[doc.id],
        storage=storage,
        settings=settings,
        tenant_id=_TENANT_A,
    )

    assert len(resp.urls) == 1
    assert resp.urls[0].source_document_id == doc.id
    assert resp.missing_ids == []
    storage.presigned_get_url.assert_awaited_once()


@pytest.mark.asyncio
@requires_db
async def test_presign_module_thumbnail_rejects_other_tenant(db_session: AsyncSession) -> None:
    module = await _seed_module(db_session, tenant_id=_TENANT_A)
    storage = _mock_storage()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "platform_service.services.source_thumbnail_service.presign_thumbnail",
            AsyncMock(return_value=("https://minio.example/thumb", 600)),
        )
        resp = await SyncService(db_session).get_module_thumbnail_presigned_urls(
            module_ids=[module.id],
            storage=storage,
            settings=Settings(minio_bucket_name=_BUCKET),
            tenant_id=_TENANT_B,
        )

    assert resp.urls == []
    assert resp.missing_ids == [module.id]


@pytest.mark.asyncio
@requires_db
async def test_presign_source_document_thumbnail_rejects_other_tenant(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session)
    await _seed_module(db_session, tenant_id=_TENANT_A, source_document_ids=[doc.id])
    storage = _mock_storage()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "platform_service.services.source_thumbnail_service.presign_thumbnail",
            AsyncMock(return_value=("https://minio.example/thumb", 600)),
        )
        resp = await SyncService(db_session).get_source_document_thumbnail_presigned_urls(
            source_document_ids=[doc.id],
            storage=storage,
            settings=Settings(minio_bucket_name=_BUCKET),
            tenant_id=_TENANT_B,
        )

    assert resp.urls == []
    assert resp.missing_ids == [doc.id]
