"""SyncService.get_published_source_documents_bundle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from mc_foundation.objectstore import PresignedObjectUrl
from platform_service.config import Settings
from platform_service.db.models.content_block import ContentBlock
from platform_service.db.models.source_page import SourcePage
from platform_service.services.sync_service import SyncService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.conftest import _seed_module, _seed_source_document
from tests.conftest import requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]

_PRESIGNED_URL = "https://minio.example/presigned"
_THUMB_URL = "https://minio.example/thumb"


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(
        db_session,
        "module_quiz_question, module, module_family, source_document",
    )
    yield


def _mock_storage() -> MagicMock:
    storage = MagicMock()

    async def _presign(
        *,
        object_name: str,
        expires_seconds: int,
        download_filename=None,
        disposition=None,
    ):
        if "thumbnails" in object_name:
            return PresignedObjectUrl(
                url=_THUMB_URL,
                bucket_name="medtronics-storage",
                object_name=object_name,
                expires_seconds=expires_seconds,
            )
        return PresignedObjectUrl(
            url=_PRESIGNED_URL,
            bucket_name="medtronics-storage",
            object_name=object_name,
            expires_seconds=expires_seconds,
        )

    storage.presigned_get_url = AsyncMock(side_effect=_presign)
    storage.stat_object = AsyncMock()
    return storage


async def test_returns_documents_for_published_modules_only(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session, sync_published_visible=True)
    published = await _seed_module(db_session, source_document_ids=[doc.id])
    await _seed_module(
        db_session, title_localized={"bn": "Draft"}, lifecycle_status="draft", source_document_ids=[doc.id]
    )

    bundle = await SyncService(db_session).get_published_source_documents_bundle(
        storage=_mock_storage(),
    )

    assert len(bundle.source_documents) == 1
    entry = bundle.source_documents[0]
    assert entry.source_document_id == doc.id
    assert entry.title == doc.title
    assert entry.original_filename == doc.original_filename
    assert entry.presigned_url == _PRESIGNED_URL
    assert entry.presigned_expires_seconds == Settings().admin_file_presigned_max_seconds
    assert bundle.missing_ids == []

    # Draft module must not expand the document set beyond published links.
    _ = published


async def test_deduplicates_documents_across_modules(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session, sync_published_visible=True)
    await _seed_module(db_session, title_localized={"bn": "Module A"}, source_document_ids=[doc.id])
    await _seed_module(db_session, title_localized={"bn": "Module B"}, source_document_ids=[doc.id])

    bundle = await SyncService(db_session).get_published_source_documents_bundle(
        storage=_mock_storage(),
    )

    assert len(bundle.source_documents) == 1
    assert bundle.source_documents[0].source_document_id == doc.id


async def test_includes_thumbnail_presigned_url_without_storage_path(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session, sync_published_visible=True)
    doc.thumbnail_storage_path = f"medtronics-storage/ingest/thumbnails/{doc.id}.png"
    await db_session.commit()
    await _seed_module(db_session, source_document_ids=[doc.id])

    bundle = await SyncService(db_session).get_published_source_documents_bundle(
        storage=_mock_storage(),
    )

    entry = bundle.source_documents[0]
    assert entry.thumbnail_presigned_url == _THUMB_URL
    assert entry.thumbnail_presigned_expires_seconds == Settings().admin_file_presigned_max_seconds
    assert not hasattr(entry, "thumbnail_storage_path")


async def test_includes_documents_cited_only_via_source_block_ids(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session, sync_published_visible=True)
    page = SourcePage(
        source_document_id=doc.id,
        page_number=3,
        markdown_content="# Page 3",
        extraction_method="text",
        extraction_quality_score=0.9,
    )
    db_session.add(page)
    await db_session.flush()
    block = ContentBlock(
        source_page_id=page.id,
        block_order=0,
        block_type="paragraph",
        content_text="Referral guidance",
    )
    db_session.add(block)
    await db_session.flush()
    await _seed_module(
        db_session,
        source_document_ids=None,
        module_json={
            "cards": [
                {
                    "title": {"bn": "Card"},
                    "body": {"bn": "Body"},
                    "source_block_ids": [str(block.id)],
                }
            ]
        },
    )

    bundle = await SyncService(db_session).get_published_source_documents_bundle(
        storage=_mock_storage(),
    )

    assert len(bundle.source_documents) == 1
    assert bundle.source_documents[0].source_document_id == doc.id
    assert bundle.source_documents[0].presigned_url == _PRESIGNED_URL


async def test_excludes_documents_when_sync_published_visible_false(db_session: AsyncSession) -> None:
    doc = await _seed_source_document(db_session, sync_published_visible=False)
    await _seed_module(db_session, source_document_ids=[doc.id])

    bundle = await SyncService(db_session).get_published_source_documents_bundle(
        storage=_mock_storage(),
    )

    assert bundle.source_documents == []
