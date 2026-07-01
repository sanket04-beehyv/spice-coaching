"""Shared fixtures for admin dashboard API tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from asyncpg import Range
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.api.admin_ingestion_runs import router as admin_ingestion_runs_router
from platform_service.api.admin_modules import router as admin_modules_router
from platform_service.api.admin_trigger_bindings import router as admin_trigger_bindings_router
from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.source_document import SourceDocument
from platform_service.deps import get_db, get_object_storage_client
from platform_service.services.object_storage import ObjectNotFoundError, PresignedObjectUrl
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

_BUCKET = "medtronics-storage"
_OBJECT_KEY = "source-documents/abc_manual.pdf"
_STORAGE_PATH = f"{_BUCKET}/{_OBJECT_KEY}"
_PRESIGNED_URL = "https://minio.example/presigned"


# ─── Per-test cleanup ──────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    """The endpoints commit, so committed state from a prior test would leak.
    Truncate the tables this file touches before each test."""
    yield
    # Fresh transaction for the truncate.
    await db_session.rollback()
    await db_session.execute(
        text(
            "TRUNCATE module_quiz_question, module, module_family, "
            "module_trigger_binding, trigger_definition, "
            "ingestion_run_step, ingestion_run, content_block, source_page, source_document "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


# ─── App + client fixtures ─────────────────────────────────────────────────


class _FakeAttachmentStorage:
    """Minimal object storage fake for module attachment PUT/GET tests."""

    bucket_name = "medtronics-storage"
    allowed_prefixes = frozenset({"uploads", "source-documents", "media", "ingest"})

    def __init__(self, *, object_exists: bool = True) -> None:
        self.object_exists = object_exists

    async def stat_object(self, object_name: str) -> None:
        if not self.object_exists:
            raise ObjectNotFoundError(f"object {object_name!r} missing")

    async def presigned_get_url(
        self,
        *,
        object_name: str,
        expires_seconds: int,
        disposition: str = "auto",
        download_filename: str | None = None,
        **kwargs: object,
    ) -> PresignedObjectUrl:
        return PresignedObjectUrl(
            url=f"https://minio.test/{object_name}?exp={expires_seconds}",
            bucket_name=self.bucket_name,
            object_name=object_name,
            expires_seconds=expires_seconds,
        )


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    """Build a minimal FastAPI app with admin dashboard routers."""
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(admin_modules_router)
    api_router.include_router(admin_trigger_bindings_router)
    api_router.include_router(admin_ingestion_runs_router)
    app_obj.include_router(api_router)
    fake_storage = _FakeAttachmentStorage()

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    app_obj.dependency_overrides[get_object_storage_client] = lambda: fake_storage
    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ─── Helpers ────────────────────────────────────────────────────────────────


def _mock_storage(*, presigned_url: str = _PRESIGNED_URL) -> MagicMock:
    storage = MagicMock()
    storage.presigned_get_url = AsyncMock(
        return_value=PresignedObjectUrl(
            url=presigned_url,
            bucket_name=_BUCKET,
            object_name=_OBJECT_KEY,
            expires_seconds=get_settings().admin_file_presigned_max_seconds,
        )
    )
    return storage


async def _seed_source_document(
    session: AsyncSession,
    *,
    title: str = "module-detail-presign-test",
    storage_path: str = _STORAGE_PATH,
    original_filename: str | None = "manual.pdf",
    sync_published_visible: bool = False,
) -> SourceDocument:
    doc = SourceDocument(
        title=title,
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        assessment_mode="with_quiz",
        authority_label="BRAC",
        original_storage_path=storage_path,
        original_filename=original_filename,
        sync_published_visible=sync_published_visible,
    )
    session.add(doc)
    await session.flush()
    await session.commit()
    return doc


async def _seed_module(
    session: AsyncSession,
    *,
    title_localized: dict[str, str] | None = None,
    description_localized: dict[str, str] | None = None,
    domain: str = "rmnch",
    lifecycle_status: str = "published",
    clinically_reviewed: bool = False,
    visibility_window: Range | None = None,
    embedding: list[float] | None = None,
    module_json: dict | None = None,
    quality_flags_jsonb: dict | None = None,
    search_metadata_jsonb: dict | None = None,
    source_document_ids: list[UUID] | None = None,
    set_family_pointer: bool = True,
) -> Module:
    family = ModuleFamily(module_code=f"f-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized=title_localized or {"bn": "Sample"},
        description_localized=description_localized,
        domain=domain,
        module_type="refresher",
        lifecycle_status=lifecycle_status,
        clinically_reviewed=clinically_reviewed,
        visibility_window=visibility_window,
        embedding=embedding,
        module_json=module_json or {"cards": [{"title": {"bn": "C1"}, "body": {"bn": "B1"}}]},
        quality_flags_jsonb=quality_flags_jsonb,
        search_metadata_jsonb=search_metadata_jsonb,
        source_document_ids=source_document_ids,
        published_at=datetime.now(UTC) if lifecycle_status == "published" else None,
    )
    session.add(module)
    await session.flush()
    if set_family_pointer and lifecycle_status == "published":
        family.current_published_module_id = module.id
        await session.flush()
    await session.commit()
    return module


def _zero_vector(dim: int = 768) -> list[float]:
    return [0.0] * dim


def _unit_basis_vector(axis: int, dim: int = 768) -> list[float]:
    v = [0.0] * dim
    v[axis % dim] = 1.0
    return v
