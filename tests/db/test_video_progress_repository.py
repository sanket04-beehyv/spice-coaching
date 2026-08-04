"""CHWVideoProgress repository monotonic upsert tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.video_progress_repository import VideoProgressRepository
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def _seed_video(session: AsyncSession) -> SourceDocument:
    doc = SourceDocument(
        title="progress-test-video",
        source_type="video",
        primary_language="bn",
        content_domain="clinical",
        original_storage_path=f"medtronics-storage/ingest/{uuid4()}.mp4",
        original_filename="clip.mp4",
        status="ingested",
    )
    session.add(doc)
    await session.flush()
    return doc


async def test_upsert_inserts_row(db_session: AsyncSession) -> None:
    doc = await _seed_video(db_session)
    repo = VideoProgressRepository(db_session)
    row = await repo.upsert(
        chw_id=42,
        source_document_id=doc.id,
        last_position_ms=1000,
        percent_watched=10.0,
        completed=False,
    )
    await db_session.commit()
    assert row.chw_id == 42
    assert row.source_document_id == doc.id
    assert row.last_position_ms == 1000
    assert row.percent_watched == 10.0
    assert row.completed is False


async def test_upsert_is_monotonic_on_out_of_order_events(db_session: AsyncSession) -> None:
    doc = await _seed_video(db_session)
    repo = VideoProgressRepository(db_session)

    await repo.upsert(
        chw_id=7,
        source_document_id=doc.id,
        last_position_ms=50_000,
        percent_watched=80.0,
        completed=True,
    )
    await db_session.commit()

    # Older / lower progress must not regress
    row = await repo.upsert(
        chw_id=7,
        source_document_id=doc.id,
        last_position_ms=5_000,
        percent_watched=10.0,
        completed=False,
    )
    await db_session.commit()
    assert row.percent_watched == 80.0
    assert row.last_position_ms == 50_000
    assert row.completed is True


async def test_upsert_takes_position_from_higher_percent(db_session: AsyncSession) -> None:
    doc = await _seed_video(db_session)
    repo = VideoProgressRepository(db_session)

    await repo.upsert(
        chw_id=9,
        source_document_id=doc.id,
        last_position_ms=1_000,
        percent_watched=20.0,
        completed=False,
    )
    await db_session.commit()

    row = await repo.upsert(
        chw_id=9,
        source_document_id=doc.id,
        last_position_ms=40_000,
        percent_watched=55.0,
        completed=False,
    )
    await db_session.commit()
    assert row.percent_watched == 55.0
    assert row.last_position_ms == 40_000


async def test_upsert_returning_refreshes_same_session(db_session: AsyncSession) -> None:
    """RETURNING must not leave a stale identity-map instance after a 2nd upsert."""
    doc = await _seed_video(db_session)
    repo = VideoProgressRepository(db_session)

    row1 = await repo.upsert(
        chw_id=11,
        source_document_id=doc.id,
        last_position_ms=10_000,
        percent_watched=25.0,
        completed=False,
    )
    row2 = await repo.upsert(
        chw_id=11,
        source_document_id=doc.id,
        last_position_ms=45_000,
        percent_watched=75.0,
        completed=True,
    )
    await db_session.commit()
    assert row1 is row2
    assert row2.percent_watched == 75.0
    assert row2.last_position_ms == 45_000
    assert row2.completed is True
