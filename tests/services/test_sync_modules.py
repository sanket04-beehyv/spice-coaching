"""SyncService.get_modules_bundle — source documents on module payloads."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.sync_service import SyncService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

_STORAGE_PATH = "medtronics-storage/source-documents/manual.pdf"
_THUMB_PATH = "medtronics-storage/ingest/thumbnails/doc.png"


async def _make_published_module(
    session: AsyncSession,
    *,
    source_document_ids: list | None,
    thumbnail_storage_path: str | None = None,
) -> Module:
    family = ModuleFamily(module_code=f"SYNC-MOD-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        lifecycle_status="published",
        module_type="refresher",
        title_bn="মডিউল",
        domain="hypertension",
        estimated_minutes=5,
        difficulty_level="basic",
        source_document_ids=source_document_ids,
        thumbnail_storage_path=thumbnail_storage_path,
    )
    session.add(module)
    await session.flush()
    family.current_published_module_id = module.id
    await session.flush()
    return module


async def _make_source_document(
    session: AsyncSession,
    *,
    title: str = "UHIS RMNCH Manual",
    thumbnail_storage_path: str | None = _THUMB_PATH,
) -> SourceDocument:
    doc = SourceDocument(
        title=title,
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        assessment_mode="with_quiz",
        authority_label="BRAC",
        version_label="2026-Q1",
        publication_date=date(2026, 1, 15),
        original_storage_path=_STORAGE_PATH,
        original_filename="manual.pdf",
        thumbnail_storage_path=thumbnail_storage_path,
    )
    session.add(doc)
    await session.flush()
    return doc


@pytest.mark.asyncio
@requires_db
async def test_modules_bundle_omits_orphaned_source_documents(db_session: AsyncSession) -> None:
    doc_a, doc_b = uuid4(), uuid4()
    with_orphan_ids = await _make_published_module(db_session, source_document_ids=[doc_a, doc_b])
    without_ids = await _make_published_module(db_session, source_document_ids=None)

    since = datetime.now(UTC) - timedelta(days=1)
    bundle = await SyncService(db_session).get_modules_bundle(since=since)

    by_id = {m.id: m for m in bundle.modules}
    assert by_id[with_orphan_ids.id].source_documents == []
    assert by_id[without_ids.id].source_documents == []


@pytest.mark.asyncio
@requires_db
async def test_modules_bundle_includes_source_document_details(db_session: AsyncSession) -> None:
    doc_a = await _make_source_document(db_session, title="Doc A")
    doc_b = await _make_source_document(
        db_session,
        title="Doc B",
        thumbnail_storage_path=None,
    )
    with_docs = await _make_published_module(
        db_session,
        source_document_ids=[doc_a.id, doc_b.id],
    )
    without_docs = await _make_published_module(db_session, source_document_ids=None)

    since = datetime.now(UTC) - timedelta(days=1)
    bundle = await SyncService(db_session).get_modules_bundle(since=since)

    by_id = {m.id: m for m in bundle.modules}
    sources = by_id[with_docs.id].source_documents
    assert len(sources) == 2
    assert [s.source_document_id for s in sources] == [doc_a.id, doc_b.id]

    first, second = sources
    assert first.title == "Doc A"
    assert first.source_type == "pdf"
    assert first.primary_language == "bn"
    assert first.content_domain == "clinical"
    assert first.assessment_mode == "with_quiz"
    assert first.authority_label == "BRAC"
    assert first.version_label == "2026-Q1"
    assert first.publication_date == date(2026, 1, 15)
    assert first.original_filename == "manual.pdf"
    assert first.has_thumbnail is True

    assert second.title == "Doc B"
    assert second.has_thumbnail is False

    assert by_id[without_docs.id].source_documents == []


@pytest.mark.asyncio
@requires_db
async def test_modules_bundle_includes_has_thumbnail(db_session: AsyncSession) -> None:
    thumb_path = "medtronics-storage/ingest/thumbnails/abc.png"
    with_thumb = await _make_published_module(
        db_session,
        source_document_ids=None,
        thumbnail_storage_path=thumb_path,
    )
    without_thumb = await _make_published_module(
        db_session, source_document_ids=None, thumbnail_storage_path=None
    )

    since = datetime.now(UTC) - timedelta(days=1)
    bundle = await SyncService(db_session).get_modules_bundle(since=since)

    by_id = {m.id: m for m in bundle.modules}
    assert by_id[with_thumb.id].has_thumbnail is True
    assert by_id[without_thumb.id].has_thumbnail is False
