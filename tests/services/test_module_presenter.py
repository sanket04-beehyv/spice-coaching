"""Unit tests for module_presenter card provenance enrichment."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from mc_contracts.admin_modules import CardSourcePageRef
from platform_service.db.models.content_block import ContentBlock
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.services.module_presenter import (
    BlockProvenanceRow,
    CardProvenanceContext,
    _presigned_url_at_page,
    cards_with_source_pages,
    render_card_provenance,
    render_card_source_pages,
    resolve_card_provenance,
    resolve_source_pages_for_blocks,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db


async def _seed_page_with_block(
    session: AsyncSession,
    *,
    page_number: int = 3,
    start_ms: int | None = None,
    end_ms: int | None = None,
    source_type: str = "pdf",
) -> tuple[SourceDocument, ContentBlock]:
    doc = SourceDocument(
        title="provenance-doc",
        source_type=source_type,
        primary_language="bn",
        content_domain="clinical",
        assessment_mode="with_quiz",
        original_storage_path="/tmp/x.pdf",
    )
    session.add(doc)
    await session.flush()
    page = SourcePage(
        source_document_id=doc.id,
        page_number=page_number,
        markdown_content="body",
        extraction_method="text",
        extraction_quality_score=0.9,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    session.add(page)
    await session.flush()
    block = ContentBlock(
        source_page_id=page.id,
        block_order=0,
        block_type="paragraph",
        content_text="block text",
    )
    session.add(block)
    await session.flush()
    await session.commit()
    return doc, block


async def _seed_second_block_on_new_page(
    session: AsyncSession,
    *,
    page_number: int,
    doc: SourceDocument | None = None,
) -> tuple[SourceDocument, ContentBlock]:
    if doc is None:
        doc = SourceDocument(
            title="provenance-doc-2",
            source_type="pdf",
            primary_language="bn",
            content_domain="clinical",
            assessment_mode="with_quiz",
            original_storage_path="/tmp/y.pdf",
        )
        session.add(doc)
        await session.flush()
    page = SourcePage(
        source_document_id=doc.id,
        page_number=page_number,
        markdown_content="body",
        extraction_method="text",
        extraction_quality_score=0.9,
    )
    session.add(page)
    await session.flush()
    block = ContentBlock(
        source_page_id=page.id,
        block_order=0,
        block_type="paragraph",
        content_text="block text",
    )
    session.add(block)
    await session.flush()
    await session.commit()
    return doc, block


class TestResolveSourcePagesForBlocks:
    def test_preserves_source_block_ids_order(self) -> None:
        doc_a = uuid4()
        doc_b = uuid4()
        block_a = uuid4()
        block_b = uuid4()
        block_c = uuid4()
        provenance = {
            block_a: BlockProvenanceRow(7, doc_a, None, None),
            block_b: BlockProvenanceRow(2, doc_a, None, None),
            block_c: BlockProvenanceRow(1, doc_b, None, None),
        }
        pages = resolve_source_pages_for_blocks([block_a, block_b, block_c], provenance)
        assert [(p.source_document_id, p.page_number) for p in pages] == [
            (doc_a, 7),
            (doc_a, 2),
            (doc_b, 1),
        ]

    def test_dedupes_same_page_while_preserving_first_occurrence(self) -> None:
        doc_id = uuid4()
        block_a = uuid4()
        block_b = uuid4()
        provenance = {
            block_a: BlockProvenanceRow(5, doc_id, None, None),
            block_b: BlockProvenanceRow(5, doc_id, None, None),
        }
        pages = resolve_source_pages_for_blocks([block_a, block_b], provenance)
        assert len(pages) == 1
        assert pages[0].page_number == 5


class TestPresignedUrlAtPage:
    def test_returns_none_without_base(self) -> None:
        assert _presigned_url_at_page(None, 75, source_type="pdf") is None

    def test_appends_page_fragment_for_pdf(self) -> None:
        base = "https://minio.example/doc.pdf?X-Amz-Signature=abc"
        assert _presigned_url_at_page(base, 75, source_type="pdf") == f"{base}#page=75"

    @pytest.mark.parametrize("source_type", ["pptx", "docx", "video", "audio", "transcript"])
    def test_does_not_append_page_fragment_for_non_pdf(self, source_type: str) -> None:
        base = "https://minio.example/doc?X-Amz-Signature=abc"
        assert _presigned_url_at_page(base, 75, source_type=source_type) == base


class TestRenderCardSourcePages:
    def test_pdf_gets_page_fragment(self) -> None:
        doc_id = uuid4()
        base = "https://minio.example/doc.pdf"
        pages = render_card_source_pages(
            [CardSourcePageRef(source_document_id=doc_id, page_number=12)],
            source_type_by_doc={doc_id: "pdf"},
            presigned_by_doc={doc_id: base},
        )
        assert pages[0]["presigned_url"] == f"{base}#page=12"

    def test_video_does_not_get_page_fragment(self) -> None:
        doc_id = uuid4()
        base = "https://minio.example/clip.mp4"
        pages = render_card_source_pages(
            [
                CardSourcePageRef(
                    source_document_id=doc_id,
                    page_number=3,
                    start_ms=60_000,
                    end_ms=120_000,
                )
            ],
            source_type_by_doc={doc_id: "video"},
            presigned_by_doc={doc_id: base},
        )
        assert pages[0]["presigned_url"] == base


class TestCardsWithSourcePages:
    pytestmark = [requires_db, pytest.mark.asyncio]

    async def test_empty_cards(self, db_session: AsyncSession) -> None:
        assert await cards_with_source_pages(db_session, []) == []

    async def test_card_without_block_ids_gets_empty_source_pages(self, db_session: AsyncSession) -> None:
        cards = [{"id": "card-0", "title": {"bn": "T"}}]
        out = await cards_with_source_pages(db_session, cards)
        assert out[0]["source_pages"] == []

    async def test_resolves_page_number_per_card(self, db_session: AsyncSession) -> None:
        doc_a, block_a = await _seed_page_with_block(db_session, page_number=2)
        doc_b, block_b = await _seed_page_with_block(db_session, page_number=7)

        cards = [
            {"id": "card-0", "source_block_ids": [str(block_a.id)]},
            {"id": "card-1", "source_block_ids": [str(block_b.id), str(block_b.id)]},
        ]
        out = await cards_with_source_pages(db_session, cards)

        assert out[0]["source_pages"] == [
            {
                "source_document_id": str(doc_a.id),
                "page_number": 2,
                "start_ms": None,
                "end_ms": None,
                "presigned_url": None,
                "presigned_expires_seconds": None,
            }
        ]
        assert out[1]["source_pages"] == [
            {
                "source_document_id": str(doc_b.id),
                "page_number": 7,
                "start_ms": None,
                "end_ms": None,
                "presigned_url": None,
                "presigned_expires_seconds": None,
            }
        ]

    async def test_source_pages_follow_source_block_ids_order(self, db_session: AsyncSession) -> None:
        doc, block_high = await _seed_page_with_block(db_session, page_number=9)
        _, block_low = await _seed_second_block_on_new_page(db_session, page_number=2, doc=doc)

        out = await cards_with_source_pages(
            db_session,
            [{"source_block_ids": [str(block_high.id), str(block_low.id)]}],
        )
        page_numbers = [p["page_number"] for p in out[0]["source_pages"]]
        assert page_numbers == [9, 2]

    async def test_dedupes_multiple_blocks_on_same_page(self, db_session: AsyncSession) -> None:
        doc = SourceDocument(
            title="multi-block",
            source_type="pdf",
            primary_language="bn",
            content_domain="clinical",
            assessment_mode="with_quiz",
            original_storage_path="/tmp/x.pdf",
        )
        db_session.add(doc)
        await db_session.flush()
        page = SourcePage(
            source_document_id=doc.id,
            page_number=5,
            markdown_content="body",
            extraction_method="text",
            extraction_quality_score=0.9,
        )
        db_session.add(page)
        await db_session.flush()
        b1 = ContentBlock(
            source_page_id=page.id,
            block_order=0,
            block_type="paragraph",
            content_text="one",
        )
        b2 = ContentBlock(
            source_page_id=page.id,
            block_order=1,
            block_type="paragraph",
            content_text="two",
        )
        db_session.add_all([b1, b2])
        await db_session.flush()
        await db_session.commit()

        out = await cards_with_source_pages(
            db_session,
            [{"source_block_ids": [str(b1.id), str(b2.id)]}],
        )
        assert len(out[0]["source_pages"]) == 1
        assert out[0]["source_pages"][0]["page_number"] == 5

    async def test_unknown_block_id_yields_empty_source_pages(self, db_session: AsyncSession) -> None:
        out = await cards_with_source_pages(
            db_session,
            [{"source_block_ids": [str(uuid4())]}],
        )
        assert out[0]["source_pages"] == []

    async def test_includes_av_timecodes(self, db_session: AsyncSession) -> None:
        doc, block = await _seed_page_with_block(
            db_session,
            page_number=1,
            start_ms=60_000,
            end_ms=120_000,
            source_type="video",
        )
        out = await cards_with_source_pages(
            db_session,
            [{"source_block_ids": [str(block.id)]}],
        )
        assert out[0]["source_pages"] == [
            {
                "source_document_id": str(doc.id),
                "page_number": 1,
                "start_ms": 60_000,
                "end_ms": 120_000,
                "presigned_url": None,
                "presigned_expires_seconds": None,
            }
        ]

    async def test_presigned_url_appends_page_fragment_for_pdf(self, db_session: AsyncSession) -> None:
        doc, block = await _seed_page_with_block(db_session, page_number=75, source_type="pdf")
        base = "https://minio.example/doc.pdf?X-Amz-Signature=abc"
        out = await cards_with_source_pages(
            db_session,
            [{"source_block_ids": [str(block.id)]}],
            presigned_by_doc={doc.id: base},
            presigned_expires_by_doc={doc.id: 3600},
        )
        page = out[0]["source_pages"][0]
        assert page["presigned_url"] == f"{base}#page=75"
        assert page["presigned_expires_seconds"] == 3600

    async def test_presigned_url_omits_page_fragment_for_video(self, db_session: AsyncSession) -> None:
        doc, block = await _seed_page_with_block(db_session, page_number=3, source_type="video")
        base = "https://minio.example/clip.mp4?X-Amz-Signature=abc"
        out = await cards_with_source_pages(
            db_session,
            [{"source_block_ids": [str(block.id)]}],
            presigned_by_doc={doc.id: base},
            presigned_expires_by_doc={doc.id: 3600},
        )
        page = out[0]["source_pages"][0]
        assert page["presigned_url"] == base

    async def test_empty_string_presigned_url_is_not_represigned(self, db_session: AsyncSession) -> None:
        doc, block = await _seed_page_with_block(db_session, page_number=4)
        storage = AsyncMock()
        with patch(
            "platform_service.services.module_presenter.SyncService.get_source_document_presigned_urls",
            new_callable=AsyncMock,
        ) as mock_presign:
            out = await cards_with_source_pages(
                db_session,
                [{"source_block_ids": [str(block.id)]}],
                storage=storage,
                presigned_by_doc={doc.id: ""},
            )
        mock_presign.assert_not_called()
        assert out[0]["source_pages"][0]["presigned_url"] is None


class TestResolveAndRenderCardProvenance:
    pytestmark = [requires_db, pytest.mark.asyncio]

    async def test_render_card_provenance_uses_resolved_context(self, db_session: AsyncSession) -> None:
        doc, block = await _seed_page_with_block(db_session, page_number=11)
        context = await resolve_card_provenance(
            db_session,
            [{"source_block_ids": [str(block.id)]}],
        )
        rendered = render_card_provenance([block.id], context)
        assert rendered[0]["page_number"] == 11
        assert rendered[0]["source_document_id"] == str(doc.id)

    async def test_resolve_card_provenance_returns_source_types(self, db_session: AsyncSession) -> None:
        doc, block = await _seed_page_with_block(db_session, source_type="pptx")
        context = await resolve_card_provenance(
            db_session,
            [{"source_block_ids": [str(block.id)]}],
        )
        assert context.source_type_by_doc[doc.id] == "pptx"


class TestRenderCardProvenanceUnit:
    def test_render_card_provenance_with_explicit_context(self) -> None:
        doc_id = uuid4()
        block_id = uuid4()
        context = CardProvenanceContext(
            provenance_by_block={
                block_id: BlockProvenanceRow(
                    page_number=4,
                    source_document_id=doc_id,
                    start_ms=None,
                    end_ms=None,
                )
            },
            source_type_by_doc={doc_id: "pdf"},
            presigned_urls={doc_id: "https://example/doc.pdf"},
            presigned_expires={doc_id: 900},
        )
        rendered = render_card_provenance([block_id], context)
        assert rendered == [
            {
                "source_document_id": str(doc_id),
                "page_number": 4,
                "start_ms": None,
                "end_ms": None,
                "presigned_url": "https://example/doc.pdf#page=4",
                "presigned_expires_seconds": 900,
            }
        ]
