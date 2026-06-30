"""Unit tests for module thumbnail defaults and path validation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from platform_service.config import Settings
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.validators import ValidationError
from platform_service.services.module_thumbnail_service import (
    resolve_default_module_thumbnail,
    validate_module_thumbnail_storage_path,
)
from platform_service.services.object_storage import ObjectNotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

_BUCKET = "medtronics-storage"


def _settings() -> Settings:
    return Settings(minio_bucket_name=_BUCKET)


@pytest.mark.asyncio
async def test_resolve_default_module_thumbnail_first_with_path() -> None:
    doc_a_id = uuid4()
    doc_b_id = uuid4()
    path_b = f"{_BUCKET}/ingest/thumbnails/{doc_b_id}.png"

    doc_a = MagicMock(spec=SourceDocument)
    doc_a.id = doc_a_id
    doc_a.thumbnail_storage_path = None
    doc_b = MagicMock(spec=SourceDocument)
    doc_b.id = doc_b_id
    doc_b.thumbnail_storage_path = path_b

    session = MagicMock(spec=AsyncSession)
    repo = MagicMock()
    repo.list_source_documents_by_ids = AsyncMock(return_value=[doc_a, doc_b])

    with patch(
        "platform_service.services.module_thumbnail_service.SourceRepository",
        return_value=repo,
    ):
        result = await resolve_default_module_thumbnail(session, [doc_a_id, doc_b_id])

    assert result == path_b
    repo.list_source_documents_by_ids.assert_awaited_once_with([doc_a_id, doc_b_id])


@pytest.mark.asyncio
async def test_resolve_default_module_thumbnail_empty_ids() -> None:
    session = MagicMock(spec=AsyncSession)
    assert await resolve_default_module_thumbnail(session, []) is None


@pytest.mark.asyncio
async def test_validate_module_thumbnail_storage_path_none_clears() -> None:
    assert await validate_module_thumbnail_storage_path(None, settings=_settings()) is None


@pytest.mark.asyncio
async def test_validate_module_thumbnail_rejects_bad_prefix() -> None:
    path = f"{_BUCKET}/media/bad.png"
    with pytest.raises(ValidationError) as exc_info:
        await validate_module_thumbnail_storage_path(path, settings=_settings())
    assert exc_info.value.code == "invalid_thumbnail_object_prefix"


@pytest.mark.asyncio
async def test_validate_module_thumbnail_accepts_ingest_path() -> None:
    path = f"{_BUCKET}/ingest/thumbnails/{uuid4()}.png"
    storage = MagicMock()
    storage.stat_object = AsyncMock()
    result = await validate_module_thumbnail_storage_path(path, settings=_settings(), storage=storage)
    assert result == path
    storage.stat_object.assert_awaited_once_with(path)


@pytest.mark.asyncio
async def test_validate_module_thumbnail_object_not_found() -> None:
    path = f"{_BUCKET}/module-thumbnails/custom.png"
    storage = MagicMock()
    storage.stat_object = AsyncMock(side_effect=ObjectNotFoundError("missing"))

    with pytest.raises(ValidationError) as exc_info:
        await validate_module_thumbnail_storage_path(path, settings=_settings(), storage=storage)
    assert exc_info.value.code == "thumbnail_object_not_found"
