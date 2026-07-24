"""SyncService.get_modules_bundle — source documents on module payloads."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.chw_module_assignment import CHWModuleAssignment
from platform_service.db.models.chw_training_request import CHWTrainingRequest
from platform_service.db.models.content_block import ContentBlock
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.services.module_card_service import (
    ModuleCardService,
    extract_cards_from_module_json,
    module_json_shell,
)
from platform_service.services.sync_service import SyncService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

_STORAGE_PATH = "medtronics-storage/source-documents/manual.pdf"
_THUMB_PATH = "medtronics-storage/ingest/thumbnails/doc.png"

_SAMPLE_SEARCH_METADATA = {
    "schema_version": 1,
    "keywords": {"en": ["cough"], "bn": []},
    "search_phrases": {"en": ["child cough"], "bn": []},
    "synonyms": {"bn": {}},
    "topic_tags": {"bn": ["respiratory"]},
    "clinical_conditions": {"bn": []},
    "audience": "chw",
    "rationale": "",
}


async def _make_published_module(
    session: AsyncSession,
    *,
    source_document_ids: list | None,
    thumbnail_storage_path: str | None = None,
    module_json: dict[str, Any] | None = None,
    search_metadata_jsonb: dict[str, Any] | None = None,
) -> Module:
    family = ModuleFamily(module_code=f"SYNC-MOD-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    if module_json is None:
        cards_data = []
        shell_json: dict | None = {}
    else:
        cards_data = extract_cards_from_module_json(module_json)
        shell_json = module_json_shell(module_json)
    module = Module(
        module_family_id=family.id,
        version=1,
        lifecycle_status="published",
        module_type="refresher",
        title_localized={"bn": "মডিউল"},
        domain="hypertension",
        estimated_minutes=5,
        difficulty_level="basic",
        source_document_ids=source_document_ids,
        thumbnail_storage_path=thumbnail_storage_path,
        module_json=shell_json,
        search_metadata_jsonb=search_metadata_jsonb,
    )
    session.add(module)
    await session.flush()
    if cards_data:
        await ModuleCardService(session).append_cards(module.id, cards_data)
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


@pytest.mark.asyncio
@requires_db
async def test_modules_bundle_cards_include_source_pages_from_block_ids(
    db_session: AsyncSession,
) -> None:
    doc = await _make_source_document(db_session)
    page = SourcePage(
        source_document_id=doc.id,
        page_number=12,
        markdown_content="# Page 12",
        extraction_method="text",
        extraction_quality_score=0.9,
    )
    db_session.add(page)
    await db_session.flush()
    block = ContentBlock(
        source_page_id=page.id,
        block_order=0,
        block_type="paragraph",
        content_text="ARI guidance",
    )
    db_session.add(block)
    await db_session.flush()

    module = await _make_published_module(
        db_session,
        source_document_ids=[doc.id],
        module_json={
            "cards": [
                {
                    "id": "card-0",
                    "title": {"bn": "C1"},
                    "body": {"bn": "B1"},
                    "source_block_ids": [str(block.id)],
                }
            ]
        },
    )

    since = datetime.now(UTC) - timedelta(days=1)
    bundle = await SyncService(db_session).get_modules_bundle(since=since)

    by_id = {m.id: m for m in bundle.modules}
    card = by_id[module.id].cards[0]
    page_ref = card["source_pages"][0]
    assert page_ref["source_document_id"] == str(doc.id)
    assert page_ref["page_number"] == 12
    assert page_ref["start_ms"] is None
    assert page_ref["end_ms"] is None
    assert page_ref["presigned_url"] is None
    assert page_ref["presigned_expires_seconds"] is None


@pytest.mark.asyncio
@requires_db
async def test_modules_bundle_includes_search_metadata(db_session: AsyncSession) -> None:
    with_metadata = await _make_published_module(
        db_session,
        source_document_ids=None,
        search_metadata_jsonb=_SAMPLE_SEARCH_METADATA,
    )
    without_metadata = await _make_published_module(
        db_session,
        source_document_ids=None,
        search_metadata_jsonb=None,
    )

    since = datetime.now(UTC) - timedelta(days=1)
    bundle = await SyncService(db_session).get_modules_bundle(since=since)

    by_id = {m.id: m for m in bundle.modules}
    assert by_id[with_metadata.id].search_metadata == _SAMPLE_SEARCH_METADATA
    assert by_id[without_metadata.id].search_metadata is None


async def _make_gap(session: AsyncSession) -> BehaviouralGap:
    code = f"gap_{uuid4().hex[:8]}"
    gap = BehaviouralGap(
        gap_code=code,
        description=code,
        domain="rmnch",
        detection_rule_jsonb={},
    )
    session.add(gap)
    await session.flush()
    return gap


@pytest.mark.asyncio
@requires_db
async def test_modules_bundle_includes_behavioural_gap_associations(
    db_session: AsyncSession,
) -> None:
    primary_gap = await _make_gap(db_session)
    secondary_gap = await _make_gap(db_session)
    with_gaps = await _make_published_module(db_session, source_document_ids=None)
    without_gaps = await _make_published_module(db_session, source_document_ids=None)

    await ModuleGapRepository(db_session).replace_links(
        with_gaps.id,
        gap_ids=[primary_gap.id, secondary_gap.id],
        primary_gap_id=primary_gap.id,
    )
    await db_session.refresh(with_gaps)

    since = datetime.now(UTC) - timedelta(days=1)
    bundle = await SyncService(db_session).get_modules_bundle(since=since)

    by_id = {m.id: m for m in bundle.modules}
    linked = by_id[with_gaps.id]
    assert linked.primary_gap_id == primary_gap.id
    assert linked.behavioural_gap_ids == [primary_gap.id, secondary_gap.id]

    unlinked = by_id[without_gaps.id]
    assert unlinked.primary_gap_id is None
    assert unlinked.behavioural_gap_ids == []


@pytest.mark.asyncio
@requires_db
async def test_modules_bundle_assigned_module_ids(db_session: AsyncSession) -> None:
    module = await _make_published_module(db_session, source_document_ids=None)
    db_session.add(
        CHWModuleAssignment(
            module_id=module.id,
            assignment_type="individual",
            user_id=1313053891,
            assigned_by=1,
        )
    )
    await db_session.commit()

    since = datetime.now(UTC) - timedelta(days=1)

    without_user = await SyncService(db_session).get_modules_bundle(since=since)
    assert without_user.assigned_module_ids == []
    assert without_user.requested_modules == []

    with_user = await SyncService(db_session).get_modules_bundle(since=since, user_id=1313053891)
    assert len(with_user.assigned_module_ids) == 1
    assert with_user.assigned_module_ids[0].module_id == module.id
    assert with_user.assigned_module_ids[0].assigned_at is not None
    assert with_user.requested_modules == []


@pytest.mark.asyncio
@requires_db
async def test_modules_bundle_requested_modules(db_session: AsyncSession) -> None:
    # Use an ID outside the hardcoded user directory so geo/PO assignment
    # leftovers from other tests cannot pollute assigned_module_ids.
    chw_id = uuid4().int % (10**15) + 1
    module = await _make_published_module(db_session, source_document_ids=None)
    earlier = datetime.now(UTC) - timedelta(hours=2)
    later = datetime.now(UTC) - timedelta(hours=1)
    known = CHWTrainingRequest(
        chw_id=chw_id,
        module_id=module.id,
        requested_module_name=None,
        reason="Need refresher",
        submitted_at=earlier,
        tenant_id=None,
    )
    custom = CHWTrainingRequest(
        chw_id=chw_id,
        module_id=None,
        requested_module_name="Diabetes Counseling Refresh",
        reason="New patient cases",
        submitted_at=later,
        tenant_id=None,
    )
    db_session.add_all([known, custom])
    await db_session.commit()

    since = datetime.now(UTC) - timedelta(days=1)

    without_user = await SyncService(db_session).get_modules_bundle(since=since)
    assert without_user.requested_modules == []

    with_user = await SyncService(db_session).get_modules_bundle(since=since, user_id=chw_id)
    assert len(with_user.requested_modules) == 2
    assert with_user.requested_modules[0].request_id == custom.id
    assert with_user.requested_modules[0].module_id is None
    assert with_user.requested_modules[0].requested_module_name == "Diabetes Counseling Refresh"
    assert with_user.requested_modules[0].reason == "New patient cases"
    assert with_user.requested_modules[1].request_id == known.id
    assert with_user.requested_modules[1].module_id == module.id
    assert with_user.requested_modules[1].requested_module_name is None
    assert with_user.requested_modules[1].reason == "Need refresher"
    # Requests stay separate from assignments (no assignment created in this test).
    assert with_user.assigned_module_ids == []


@pytest.mark.asyncio
@requires_db
async def test_modules_bundle_requested_modules_tenant_scope(db_session: AsyncSession) -> None:
    chw_id = uuid4().int % (10**15) + 1
    tenant_a = uuid4()
    tenant_b = uuid4()
    global_row = CHWTrainingRequest(
        chw_id=chw_id,
        module_id=None,
        requested_module_name="Global Request",
        reason=None,
        submitted_at=datetime.now(UTC) - timedelta(hours=3),
        tenant_id=None,
    )
    matching = CHWTrainingRequest(
        chw_id=chw_id,
        module_id=None,
        requested_module_name="Tenant A Request",
        reason=None,
        submitted_at=datetime.now(UTC) - timedelta(hours=2),
        tenant_id=tenant_a,
    )
    other = CHWTrainingRequest(
        chw_id=chw_id,
        module_id=None,
        requested_module_name="Tenant B Request",
        reason=None,
        submitted_at=datetime.now(UTC) - timedelta(hours=1),
        tenant_id=tenant_b,
    )
    db_session.add_all([global_row, matching, other])
    await db_session.commit()

    since = datetime.now(UTC) - timedelta(days=1)
    scoped = await SyncService(db_session).get_modules_bundle(
        since=since,
        user_id=chw_id,
        tenant_id=tenant_a,
    )
    names = {row.requested_module_name for row in scoped.requested_modules}
    assert names == {"Global Request", "Tenant A Request"}
    assert "Tenant B Request" not in names
