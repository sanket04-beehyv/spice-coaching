"""Unit tests for ingest start orchestration."""

from __future__ import annotations

from uuid import uuid4

import pytest
from mc_foundation.problem import AppError
from platform_service.db.models.ingest_batch import IngestBatch
from platform_service.db.models.source_document import SourceDocument
from platform_service.services.ingest_start_service import IngestStartParams, IngestStartService
from platform_service.services.ingest_upload_service import IngestUploadService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def _seed_uploaded(db_session: AsyncSession, *, title: str = "Staged") -> SourceDocument:
    doc = SourceDocument(
        title=title,
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        original_storage_path="bucket/ingest/staged.pdf",
        original_filename="staged.pdf",
        status="uploaded",
    )
    db_session.add(doc)
    await db_session.flush()
    return doc


async def _seed_ingested(db_session: AsyncSession) -> SourceDocument:
    doc = SourceDocument(
        title="Done",
        source_type="pdf",
        primary_language="bn",
        content_domain="clinical",
        original_storage_path="bucket/ingest/done.pdf",
        original_filename="done.pdf",
        content_sha256="abc123",
        status="ingested",
    )
    db_session.add(doc)
    await db_session.flush()
    return doc


def _params() -> IngestStartParams:
    return IngestStartParams(
        assessment_mode="with_quiz",
        uploaded_by="tester",
    )


async def test_start_uploaded_document_sets_ingesting(db_session: AsyncSession) -> None:
    staged = await _seed_uploaded(db_session)
    staged.content_domain = "digital"
    await db_session.flush()
    service = IngestStartService(db_session)

    result = await service.start(
        source_document_ids=[staged.id],
        params=_params(),
        override_flags=[False],
    )

    assert len(result.sources) == 1
    assert result.sources[0].source_document_id == staged.id
    await db_session.refresh(staged)
    assert staged.status == "ingesting"
    assert staged.content_domain == "digital"

    batch = await db_session.get(IngestBatch, result.batch_id)
    assert batch is not None
    assert batch.assessment_mode == "with_quiz"
    assert batch.ingestion_instructions is None
    assert batch.cards_per_module is None
    assert batch.quizzes_per_module is None


async def test_start_missing_source_raises_not_found(db_session: AsyncSession) -> None:
    service = IngestStartService(db_session)

    with pytest.raises(AppError) as exc_info:
        await service.start(
            source_document_ids=[uuid4()],
            params=_params(),
            override_flags=[False],
        )

    assert exc_info.value.code == "source_not_found"


async def test_start_ingested_without_override_raises(db_session: AsyncSession) -> None:
    ingested = await _seed_ingested(db_session)
    service = IngestStartService(db_session)

    with pytest.raises(AppError) as exc_info:
        await service.start(
            source_document_ids=[ingested.id],
            params=_params(),
            override_flags=[False],
        )

    assert exc_info.value.code == "duplicate_content"
    assert exc_info.value.status == 409
    conflicts = exc_info.value.extensions["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["content_sha256"] == "abc123"
    assert conflicts[0]["existing_source_documents"][0]["source_document_id"] == str(ingested.id)


async def test_start_mixed_batch_skips_ingested_without_override(db_session: AsyncSession) -> None:
    ingested = await _seed_ingested(db_session)
    staged = await _seed_uploaded(db_session, title="Fresh")
    service = IngestStartService(db_session)

    result = await service.start(
        source_document_ids=[ingested.id, staged.id],
        params=_params(),
        override_flags=[False, False],
    )

    assert len(result.sources) == 1
    assert result.sources[0].source_document_id == staged.id
    assert len(result.skipped_duplicates) == 1
    assert result.skipped_duplicates[0].content_sha256 == "abc123"
    assert result.skipped_duplicates[0].existing_source_documents[0].id == ingested.id


async def test_start_ingested_with_override_clones_row(db_session: AsyncSession) -> None:
    ingested = await _seed_ingested(db_session)
    ingested.content_domain = "digital"
    await db_session.flush()
    service = IngestStartService(db_session)

    result = await service.start(
        source_document_ids=[ingested.id],
        params=_params(),
        override_flags=[True],
    )

    assert len(result.sources) == 1
    assert result.sources[0].source_document_id != ingested.id
    clone = await db_session.get(SourceDocument, result.sources[0].source_document_id)
    assert clone is not None
    assert clone.content_domain == "digital"


async def test_resolve_override_flags_length_mismatch(db_session: AsyncSession) -> None:
    staged = await _seed_uploaded(db_session)

    with pytest.raises(Exception) as exc_info:
        IngestUploadService.resolve_override_duplicates_for_ids([True, False], [staged.id])

    assert "override_duplicates must have 1 entries" in str(exc_info.value)
