"""SourceRepository tests for the Stage 1 vision-recovery surface.

Covers the two methods added for the vision_failed recovery pass:

- ``list_vision_failed_pages`` — returns SourcePage rows whose Stage 1
  vision call raised on the main loop, ordered by page_number.
- ``update_page_extraction`` — replaces a page's extraction output
  in-place (used by the recovery pass to upgrade vision_failed → vision).

Both methods are DB-backed; tests use the platform's `db_session` fixture
so they share the same per-loop engine as production code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.models.source_page import SourcePage
from platform_service.db.repositories.source_repository import SourceRepository
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text("TRUNCATE content_block, source_page, source_document RESTART IDENTITY CASCADE")
    )
    await db_session.commit()


# ─── Helpers ───────────────────────────────────────────────────────────────


async def _seed_doc(session: AsyncSession) -> UUID:
    sd = SourceDocument(
        title="recovery-test",
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        assessment_mode="with_quiz",
        authority_label="BRAC",
        original_storage_path="/tmp/x.pdf",
    )
    session.add(sd)
    await session.flush()
    await session.commit()
    return sd.id


async def _seed_page(
    repo: SourceRepository,
    *,
    document_id: UUID,
    page_number: int,
    extraction_method: str,
    markdown_content: str = "stub",
    extraction_quality_score: float = 0.5,
    page_image_path: str | None = None,
) -> UUID:
    page = await repo.create_source_page(
        source_document_id=document_id,
        page_number=page_number,
        markdown_content=markdown_content,
        extraction_method=extraction_method,
        extraction_quality_score=extraction_quality_score,
        page_image_path=page_image_path,
        language_detected="bn",
    )
    return page.id


# ─── list_vision_failed_pages ───────────────────────────────────────────────


class TestListVisionFailedPages:
    async def test_returns_only_vision_failed_rows_for_the_document(self, db_session: AsyncSession) -> None:
        repo = SourceRepository(db_session)
        doc_id = await _seed_doc(db_session)

        await _seed_page(repo, document_id=doc_id, page_number=1, extraction_method="text")
        page2_id = await _seed_page(
            repo, document_id=doc_id, page_number=2, extraction_method="vision_failed"
        )
        await _seed_page(repo, document_id=doc_id, page_number=3, extraction_method="vision")
        page4_id = await _seed_page(
            repo, document_id=doc_id, page_number=4, extraction_method="vision_failed"
        )
        await db_session.commit()

        rows = await repo.list_vision_failed_pages(doc_id)
        assert [p.id for p in rows] == [page2_id, page4_id]
        assert all(p.extraction_method == "vision_failed" for p in rows)

    async def test_ordered_by_page_number(self, db_session: AsyncSession) -> None:
        """The recovery pass iterates serially in page-number order so log
        output is reader-friendly. Verify the ordering contract."""
        repo = SourceRepository(db_session)
        doc_id = await _seed_doc(db_session)
        # Insert out of order to prove the query (not insertion order) sorts.
        for pn in (10, 1, 5, 3, 7):
            await _seed_page(repo, document_id=doc_id, page_number=pn, extraction_method="vision_failed")
        await db_session.commit()

        rows = await repo.list_vision_failed_pages(doc_id)
        assert [p.page_number for p in rows] == [1, 3, 5, 7, 10]

    async def test_returns_empty_list_when_no_failures(self, db_session: AsyncSession) -> None:
        repo = SourceRepository(db_session)
        doc_id = await _seed_doc(db_session)
        await _seed_page(repo, document_id=doc_id, page_number=1, extraction_method="text")
        await db_session.commit()

        assert await repo.list_vision_failed_pages(doc_id) == []

    async def test_scoped_to_document(self, db_session: AsyncSession) -> None:
        """A vision_failed page in document A must not appear in document B's
        recovery list — the recovery pass is per-run."""
        repo = SourceRepository(db_session)
        doc_a = await _seed_doc(db_session)
        doc_b = await _seed_doc(db_session)

        await _seed_page(repo, document_id=doc_a, page_number=1, extraction_method="vision_failed")
        await _seed_page(repo, document_id=doc_b, page_number=1, extraction_method="vision_failed")
        await db_session.commit()

        rows_a = await repo.list_vision_failed_pages(doc_a)
        rows_b = await repo.list_vision_failed_pages(doc_b)
        assert len(rows_a) == 1 and rows_a[0].source_document_id == doc_a
        assert len(rows_b) == 1 and rows_b[0].source_document_id == doc_b


# ─── update_page_extraction ─────────────────────────────────────────────────


class TestUpdatePageExtraction:
    async def test_replaces_markdown_method_and_quality_score(self, db_session: AsyncSession) -> None:
        """Happy path: a vision_failed row gets upgraded to method='vision'
        with the recovered markdown and a fresh quality score."""
        repo = SourceRepository(db_session)
        doc_id = await _seed_doc(db_session)
        page_id = await _seed_page(
            repo,
            document_id=doc_id,
            page_number=1,
            extraction_method="vision_failed",
            markdown_content="garbage Bijoy text",
            extraction_quality_score=0.05,
        )
        await db_session.commit()

        await repo.update_page_extraction(
            page_id,
            markdown_content="# Recovered heading\n\nClean Bangla content.",
            extraction_method="vision",
            extraction_quality_score=0.92,
        )
        await db_session.commit()

        row = (await db_session.execute(select(SourcePage).where(SourcePage.id == page_id))).scalar_one()
        assert row.extraction_method == "vision"
        assert row.markdown_content == "# Recovered heading\n\nClean Bangla content."
        assert row.extraction_quality_score == pytest.approx(0.92)

    async def test_preserves_existing_image_path_when_arg_omitted(self, db_session: AsyncSession) -> None:
        """If the recovery pass uses the cached PNG, it doesn't pass
        `page_image_path` — the row's existing path must be preserved."""
        repo = SourceRepository(db_session)
        doc_id = await _seed_doc(db_session)
        page_id = await _seed_page(
            repo,
            document_id=doc_id,
            page_number=1,
            extraction_method="vision_failed",
            page_image_path="/tmp/cached/page_0001.png",
        )
        await db_session.commit()

        await repo.update_page_extraction(
            page_id,
            markdown_content="# OK",
            extraction_method="vision",
            extraction_quality_score=0.8,
        )
        await db_session.commit()

        row = (await db_session.execute(select(SourcePage).where(SourcePage.id == page_id))).scalar_one()
        assert row.page_image_path == "/tmp/cached/page_0001.png"

    async def test_overwrites_image_path_when_provided(self, db_session: AsyncSession) -> None:
        """When the recovery pass had to re-render (cached PNG missing), it
        passes the new path. The row's path must be updated."""
        repo = SourceRepository(db_session)
        doc_id = await _seed_doc(db_session)
        page_id = await _seed_page(
            repo,
            document_id=doc_id,
            page_number=1,
            extraction_method="vision_failed",
            page_image_path="/tmp/old.png",
        )
        await db_session.commit()

        await repo.update_page_extraction(
            page_id,
            markdown_content="# OK",
            extraction_method="vision",
            extraction_quality_score=0.8,
            page_image_path="/tmp/new.png",
        )
        await db_session.commit()

        row = (await db_session.execute(select(SourcePage).where(SourcePage.id == page_id))).scalar_one()
        assert row.page_image_path == "/tmp/new.png"

    async def test_unknown_page_id_raises_value_error(self, db_session: AsyncSession) -> None:
        repo = SourceRepository(db_session)
        bogus = uuid4()
        with pytest.raises(ValueError, match=str(bogus)):
            await repo.update_page_extraction(
                bogus,
                markdown_content="x",
                extraction_method="vision",
                extraction_quality_score=0.5,
            )

    async def test_after_update_page_no_longer_in_vision_failed_list(self, db_session: AsyncSession) -> None:
        """End-to-end recovery: list → update → re-list. The recovered page
        falls out of the failure list, which is how the tolerance check
        terminates."""
        repo = SourceRepository(db_session)
        doc_id = await _seed_doc(db_session)
        for pn in (1, 2):
            await _seed_page(repo, document_id=doc_id, page_number=pn, extraction_method="vision_failed")
        await db_session.commit()

        before = await repo.list_vision_failed_pages(doc_id)
        assert len(before) == 2

        await repo.update_page_extraction(
            before[0].id,
            markdown_content="# OK",
            extraction_method="vision",
            extraction_quality_score=0.9,
        )
        await db_session.commit()

        after = await repo.list_vision_failed_pages(doc_id)
        assert len(after) == 1
        assert after[0].page_number == 2

    async def test_update_thumbnail_storage_path(self, db_session: AsyncSession) -> None:
        repo = SourceRepository(db_session)
        doc_id = await _seed_doc(db_session)
        path = "medtronics-storage/ingest/thumbnails/abc.png"
        await repo.update_thumbnail_storage_path(doc_id, path)
        await db_session.commit()

        result = await db_session.execute(select(SourceDocument).where(SourceDocument.id == doc_id))
        doc = result.scalar_one()
        assert doc.thumbnail_storage_path == path
