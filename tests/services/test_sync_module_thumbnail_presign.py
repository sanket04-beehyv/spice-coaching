"""SyncService.get_module_thumbnail_presigned_urls — batch presign for device sync."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from platform_service.config import Settings
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.services.sync_service import SyncService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

_BUCKET = "medtronics-storage"
_THUMB_PATH = f"{_BUCKET}/ingest/thumbnails/{uuid4()}.png"


@pytest_asyncio.fixture(autouse=True)
async def _rollback_modules(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()


async def _seed_module(session: AsyncSession, *, thumbnail_storage_path: str | None) -> Module:
    family = ModuleFamily(module_code=f"sync-thumb-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        title_bn="thumb test",
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        thumbnail_storage_path=thumbnail_storage_path,
    )
    session.add(module)
    await session.flush()
    await session.commit()
    return module


def _mock_storage() -> MagicMock:
    storage = MagicMock()
    storage.presigned_get_url = AsyncMock()
    return storage


@pytest.mark.asyncio
@requires_db
async def test_presign_module_thumbnail_found(db_session: AsyncSession) -> None:
    module = await _seed_module(db_session, thumbnail_storage_path=_THUMB_PATH)
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
        )

    assert len(resp.urls) == 1
    assert resp.urls[0].module_id == module.id
    assert resp.urls[0].storage_path == _THUMB_PATH
    assert resp.urls[0].presigned_url == "https://minio.example/thumb"
    assert resp.missing_ids == []


@pytest.mark.asyncio
@requires_db
async def test_presign_module_thumbnail_missing_when_no_path(db_session: AsyncSession) -> None:
    module = await _seed_module(db_session, thumbnail_storage_path=None)
    storage = _mock_storage()

    resp = await SyncService(db_session).get_module_thumbnail_presigned_urls(
        module_ids=[module.id],
        storage=storage,
        settings=Settings(minio_bucket_name=_BUCKET),
    )

    assert resp.urls == []
    assert resp.missing_ids == [module.id]
