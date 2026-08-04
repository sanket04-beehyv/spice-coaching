"""Unit tests for the coaching RAG attribution helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from mc_contracts.coaching_rag import SourcePageRef
from platform_service.config import Settings
from platform_service.db.models.module import Module
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.coaching_rag_service import CoachingRagService
from sqlalchemy.ext.asyncio import AsyncSession

_block_ids_from_modules = CoachingRagService._block_ids_from_modules
_collect_source_document_links = CoachingRagService._collect_source_document_links
_provenance_per_document = CoachingRagService._provenance_per_document


def _module(*, module_id, source_document_ids: list, cards: list[dict] | None = None) -> Module:
    return Module(
        id=module_id,
        module_family_id=uuid4(),
        version=1,
        title_localized={"bn": "t"},
        domain="clinical",
        lifecycle_status="published",
        source_document_ids=source_document_ids,
        module_json={"cards": cards or []},
    )


def _service() -> CoachingRagService:
    session = MagicMock(spec=AsyncSession)
    ai = MagicMock()
    storage = MagicMock()
    storage.object_name_from_reference = MagicMock(side_effect=ValueError("unused"))
    return CoachingRagService(
        session,
        ai,
        storage,
        settings=Settings(object_storage_bucket_name="medtronics-storage"),
    )


def _source_doc(*, doc_id, title: str = "doc") -> SourceDocument:
    doc = MagicMock(spec=SourceDocument)
    doc.id = doc_id
    doc.title = title
    doc.source_type = "pdf"
    doc.original_storage_path = "/tmp/legacy.pdf"
    doc.original_filename = "legacy.pdf"
    doc.content_sha256 = None
    return doc


# ─── _collect_source_document_links ──────────────────────────────────────


def test_collect_docs_unions_all_doc_ids_from_modules() -> None:
    doc_a, doc_b, doc_c = uuid4(), uuid4(), uuid4()
    m1 = _module(module_id=uuid4(), source_document_ids=[doc_a, doc_b])
    m2 = _module(module_id=uuid4(), source_document_ids=[doc_b, doc_c])

    doc_set, per_doc = _collect_source_document_links([m1, m2])

    assert doc_set == {doc_a, doc_b, doc_c}
    assert per_doc[doc_a] == [m1.id]
    assert set(per_doc[doc_b]) == {m1.id, m2.id}
    assert per_doc[doc_c] == [m2.id]


def test_collect_docs_only_includes_cited_modules() -> None:
    """Attribution doc set is built solely from the modules passed in (cited)."""
    doc_a, doc_b = uuid4(), uuid4()
    m1 = _module(module_id=uuid4(), source_document_ids=[doc_a])
    m2 = _module(module_id=uuid4(), source_document_ids=[doc_b])

    doc_set, per_doc = _collect_source_document_links([m1])  # only cited m1

    assert doc_set == {doc_a}
    assert per_doc.get(doc_a) == [m1.id]
    assert doc_b not in doc_set
    assert m2.id not in (per_doc.get(doc_a) or [])


def test_collect_docs_skips_modules_with_no_source_documents() -> None:
    m1 = _module(module_id=uuid4(), source_document_ids=[])
    doc_set, per_doc = _collect_source_document_links([m1])
    assert doc_set == set()
    assert per_doc == {}


# ─── _block_ids_from_modules ─────────────────────────────────────────────


def test_block_ids_extracts_uuids_from_card_source_block_ids() -> None:
    b1, b2 = uuid4(), uuid4()
    mid = uuid4()
    m = _module(module_id=mid, source_document_ids=[])
    cards_by_module = {
        mid: [
            {"source_block_ids": [str(b1), str(b2)]},
            {"source_block_ids": [str(b1)]},
        ]
    }
    out = _block_ids_from_modules([m], cards_by_module)
    # duplicates are preserved (the consumer dedupes downstream via the JOIN).
    assert out == [b1, b2, b1]


def test_block_ids_skips_invalid_uuid_strings() -> None:
    b1 = uuid4()
    mid = uuid4()
    m = _module(module_id=mid, source_document_ids=[])
    cards_by_module = {
        mid: [
            {"source_block_ids": [str(b1), "not-a-uuid", None]},
        ]
    }
    out = _block_ids_from_modules([m], cards_by_module)
    assert out == [b1]


def test_block_ids_handles_non_dict_card_entries() -> None:
    mid = uuid4()
    m = _module(module_id=mid, source_document_ids=[])
    assert _block_ids_from_modules([m], {mid: [None, "junk", 42]}) == []


# ─── _provenance_per_document ─────────────────────────────────────────────


def test_provenance_per_document_dedupes_pages_and_preserves_timecodes() -> None:
    doc = uuid4()
    rows = [
        (uuid4(), 3, doc, 120_000, 240_000),
        (uuid4(), 1, doc, None, None),
        (uuid4(), 3, doc, 120_000, 240_000),  # duplicate page; second ref ignored
    ]
    page_nums, page_refs = _provenance_per_document(rows)

    assert page_nums == {doc: [1, 3]}
    refs = page_refs[doc]
    assert len(refs) == 2
    assert refs[0] == SourcePageRef(page_number=1, start_ms=None, end_ms=None)
    assert refs[1] == SourcePageRef(page_number=3, start_ms=120_000, end_ms=240_000)


def test_provenance_per_document_empty_rows_returns_empty() -> None:
    page_nums, page_refs = _provenance_per_document([])
    assert page_nums == {}
    assert page_refs == {}


def test_provenance_per_document_groups_by_doc_id() -> None:
    doc_a, doc_b = uuid4(), uuid4()
    rows = [
        (uuid4(), 1, doc_a, None, None),
        (uuid4(), 2, doc_b, 0, 60_000),
        (uuid4(), 2, doc_a, None, None),
    ]
    page_nums, page_refs = _provenance_per_document(rows)
    assert page_nums == {doc_a: [1, 2], doc_b: [2]}
    assert {r.page_number for r in page_refs[doc_a]} == {1, 2}
    assert page_refs[doc_b][0].start_ms == 0


# ─── _build_attribution ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_attribution_uses_only_cited_modules() -> None:
    doc_a, doc_b = uuid4(), uuid4()
    m_cited = _module(module_id=uuid4(), source_document_ids=[doc_a])
    m_uncited = _module(module_id=uuid4(), source_document_ids=[doc_b])
    block_id = uuid4()

    svc = _service()
    module_repo = MagicMock()
    module_repo.list_modules_by_ids = AsyncMock(return_value=[m_cited])
    module_repo.list_cards_for_module_ids = AsyncMock(return_value=[])

    source_repo = MagicMock()
    source_repo.list_block_provenance_by_ids = AsyncMock(return_value=[(block_id, 2, doc_a, None, None)])
    source_repo.list_source_documents_by_ids = AsyncMock(
        return_value=[_source_doc(doc_id=doc_a, title="cited-doc")]
    )

    with (
        patch(
            "platform_service.services.coaching_rag_service.ModuleRepository",
            return_value=module_repo,
        ),
        patch(
            "platform_service.services.coaching_rag_service.SourceRepository",
            return_value=source_repo,
        ),
    ):
        attrs = await svc._build_attribution(
            cited_ids=[m_cited.id],
            ttl=60,
            cards_by_module={
                m_cited.id: [{"source_block_ids": [str(block_id)]}],
                m_uncited.id: [{"source_block_ids": [str(uuid4())]}],
            },
        )

    assert len(attrs) == 1
    assert attrs[0].source_document_id == doc_a
    assert attrs[0].linked_module_ids == [m_cited.id]
    assert attrs[0].page_numbers == [2]
    source_repo.list_source_documents_by_ids.assert_awaited_once()
    called_ids = set(source_repo.list_source_documents_by_ids.await_args.args[0])
    assert called_ids == {doc_a}


@pytest.mark.asyncio
async def test_build_attribution_resolves_cited_not_in_cards_and_fetches_cards() -> None:
    """Cited module outside the preloaded cards_by_module still gets attributed."""
    doc_id = uuid4()
    block_id = uuid4()
    m = _module(module_id=uuid4(), source_document_ids=[doc_id])

    card_row = MagicMock()
    card_row.module_id = m.id

    svc = _service()
    module_repo = MagicMock()
    module_repo.list_modules_by_ids = AsyncMock(return_value=[m])
    module_repo.list_cards_for_module_ids = AsyncMock(return_value=[card_row])

    source_repo = MagicMock()
    source_repo.list_block_provenance_by_ids = AsyncMock(return_value=[(block_id, 1, doc_id, None, None)])
    source_repo.list_source_documents_by_ids = AsyncMock(
        return_value=[_source_doc(doc_id=doc_id, title="outside-retrieval")]
    )

    with (
        patch(
            "platform_service.services.coaching_rag_service.ModuleRepository",
            return_value=module_repo,
        ),
        patch(
            "platform_service.services.coaching_rag_service.SourceRepository",
            return_value=source_repo,
        ),
        patch(
            "platform_service.services.coaching_rag_service.card_row_to_dict",
            return_value={"source_block_ids": [str(block_id)]},
        ),
    ):
        attrs = await svc._build_attribution(
            cited_ids=[m.id],
            ttl=60,
            cards_by_module={},  # not preloaded — simulates cite outside retrieval
        )

    module_repo.list_cards_for_module_ids.assert_awaited_once_with([m.id])
    assert len(attrs) == 1
    assert attrs[0].source_document_id == doc_id
    assert attrs[0].page_numbers == [1]
    assert attrs[0].linked_module_ids == [m.id]


@pytest.mark.asyncio
async def test_build_attribution_warns_and_skips_unresolved_cited_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    missing = uuid4()
    svc = _service()
    module_repo = MagicMock()
    module_repo.list_modules_by_ids = AsyncMock(return_value=[])
    module_repo.list_cards_for_module_ids = AsyncMock(return_value=[])

    source_repo = MagicMock()
    source_repo.list_block_provenance_by_ids = AsyncMock(return_value=[])
    source_repo.list_source_documents_by_ids = AsyncMock(return_value=[])

    with (
        patch(
            "platform_service.services.coaching_rag_service.ModuleRepository",
            return_value=module_repo,
        ),
        patch(
            "platform_service.services.coaching_rag_service.SourceRepository",
            return_value=source_repo,
        ),
        caplog.at_level("WARNING"),
    ):
        attrs = await svc._build_attribution(
            cited_ids=[missing],
            ttl=60,
            cards_by_module={},
        )

    assert attrs == []
    assert any(str(missing) in r.message for r in caplog.records)
