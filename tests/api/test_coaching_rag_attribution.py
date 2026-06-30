"""Unit tests for the coaching RAG attribution helpers."""

from uuid import uuid4

from mc_contracts.coaching_rag import SourcePageRef
from platform_service.db.models.module import Module
from platform_service.services.coaching_rag_service import CoachingRagService

_attribution_seed_module_ids = CoachingRagService._attribution_seed_module_ids
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


# ─── _attribution_seed_module_ids ────────────────────────────────────────


def test_seed_prefers_cited_when_present() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    assert _attribution_seed_module_ids(cited_ids=[a], retrieved_module_ids=[a, b, c]) == [a]


def test_seed_falls_back_to_retrieved_when_no_citations() -> None:
    a, b = uuid4(), uuid4()
    assert _attribution_seed_module_ids(cited_ids=[], retrieved_module_ids=[a, b]) == [a, b]


# ─── _collect_source_document_links ──────────────────────────────────────


def test_collect_docs_unions_all_doc_ids_when_no_filter() -> None:
    doc_a, doc_b, doc_c = uuid4(), uuid4(), uuid4()
    m1 = _module(module_id=uuid4(), source_document_ids=[doc_a, doc_b])
    m2 = _module(module_id=uuid4(), source_document_ids=[doc_b, doc_c])

    doc_set, per_doc = _collect_source_document_links(
        [(m1, 0.1), (m2, 0.2)],
        link_filter_module_ids=None,
    )

    assert doc_set == {doc_a, doc_b, doc_c}
    assert per_doc[doc_a] == [m1.id]
    assert set(per_doc[doc_b]) == {m1.id, m2.id}
    assert per_doc[doc_c] == [m2.id]


def test_collect_docs_with_filter_restricts_link_list_but_keeps_doc_set() -> None:
    """A cited filter restricts which modules surface against each doc but
    must NOT drop the doc from the response when an uncited module is the
    only source — attribution is too important to disappear silently."""
    doc_a, doc_b = uuid4(), uuid4()
    m1 = _module(module_id=uuid4(), source_document_ids=[doc_a])
    m2 = _module(module_id=uuid4(), source_document_ids=[doc_b])

    doc_set, per_doc = _collect_source_document_links(
        [(m1, 0.1), (m2, 0.2)],
        link_filter_module_ids={m1.id},  # only m1 is "cited"
    )

    assert doc_set == {doc_a, doc_b}
    assert per_doc.get(doc_a) == [m1.id]
    # doc_b's only module was filtered out — but the doc id still appears in doc_set.
    assert doc_b not in per_doc


def test_collect_docs_skips_modules_with_no_source_documents() -> None:
    m1 = _module(module_id=uuid4(), source_document_ids=[])
    doc_set, per_doc = _collect_source_document_links([(m1, 0.1)], link_filter_module_ids=None)
    assert doc_set == set()
    assert per_doc == {}


# ─── _block_ids_from_modules ─────────────────────────────────────────────


def test_block_ids_extracts_uuids_from_card_source_block_ids() -> None:
    b1, b2 = uuid4(), uuid4()
    m = _module(
        module_id=uuid4(),
        source_document_ids=[],
        cards=[
            {"source_block_ids": [str(b1), str(b2)]},
            {"source_block_ids": [str(b1)]},
        ],
    )
    out = _block_ids_from_modules([m])
    # duplicates are preserved (the consumer dedupes downstream via the JOIN).
    assert out == [b1, b2, b1]


def test_block_ids_skips_invalid_uuid_strings() -> None:
    b1 = uuid4()
    m = _module(
        module_id=uuid4(),
        source_document_ids=[],
        cards=[
            {"source_block_ids": [str(b1), "not-a-uuid", None]},
        ],
    )
    out = _block_ids_from_modules([m])
    assert out == [b1]


def test_block_ids_handles_non_dict_card_entries() -> None:
    m = _module(module_id=uuid4(), source_document_ids=[], cards=[None, "junk", 42])
    assert _block_ids_from_modules([m]) == []


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
