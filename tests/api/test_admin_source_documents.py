"""Admin source document catalog API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.conftest import _seed_source_document
from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


class TestListSourceDocuments:
    async def test_defaults_to_ingested_only(self, client: AsyncClient, db_session: AsyncSession) -> None:
        ingested = await _seed_source_document(db_session, title="ready-doc")
        ingested.status = "ingested"
        await _seed_source_document(db_session, title="still-ingesting")
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents"))
        assert resp.status_code == 200
        body = resp.json()
        rows = body["source_documents"]
        assert len(rows) == 1
        assert rows[0]["id"] == str(ingested.id)
        assert rows[0]["title"] == "ready-doc"
        assert rows[0]["status"] == "ingested"
        assert body["total_source_documents"] == 1
        assert body["total_pages"] == 1
        assert body["limit"] == 50
        assert body["offset"] == 0

    async def test_orders_by_ingested_at_desc(self, client: AsyncClient, db_session: AsyncSession) -> None:
        older = await _seed_source_document(db_session, title="older")
        older.status = "ingested"
        older.ingested_at = datetime.now(UTC) - timedelta(days=2)
        newer = await _seed_source_document(db_session, title="newer")
        newer.status = "ingested"
        newer.ingested_at = datetime.now(UTC)
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents"))
        titles = [row["title"] for row in resp.json()["source_documents"]]
        assert titles == ["newer", "older"]

    async def test_status_filter_ingesting(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_source_document(db_session, title="in-flight")
        ingested = await _seed_source_document(db_session, title="done")
        ingested.status = "ingested"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?status=ingesting"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"in-flight"}
        assert body["total_source_documents"] == 1

    async def test_invalid_status_rejected(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/source-documents?status=unknown"))
        assert resp.status_code == 422

    async def test_source_type_video_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        pdf = await _seed_source_document(db_session, title="pdf-doc", original_filename="guide.pdf")
        pdf.status = "ingested"
        video = await _seed_source_document(db_session, title="video-doc", original_filename="clip.mp4")
        video.status = "ingested"
        video.source_type = "video"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?source_type=video"))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["source_documents"]) == 1
        assert body["source_documents"][0]["id"] == str(video.id)
        assert body["source_documents"][0]["source_type"] == "video"
        assert body["total_source_documents"] == 1

    async def test_multiple_source_types(self, client: AsyncClient, db_session: AsyncSession) -> None:
        pdf = await _seed_source_document(db_session, title="pdf-doc", original_filename="guide.pdf")
        pdf.status = "ingested"
        video = await _seed_source_document(db_session, title="video-doc", original_filename="clip.mp4")
        video.status = "ingested"
        video.source_type = "video"
        audio = await _seed_source_document(db_session, title="audio-doc", original_filename="clip.mp3")
        audio.status = "ingested"
        audio.source_type = "audio"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?source_type=video&source_type=audio"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["source_type"] for row in body["source_documents"]} == {"video", "audio"}
        assert body["total_source_documents"] == 2

        resp = await client.get(platform_path("/admin/source-documents?source_type=video,audio"))
        assert resp.status_code == 200
        assert resp.json()["total_source_documents"] == 2

    async def test_invalid_source_type_rejected(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/source-documents?source_type=avi"))
        assert resp.status_code == 422

        resp = await client.get(platform_path("/admin/source-documents?source_type=video&source_type=avi"))
        assert resp.status_code == 422

    async def test_filename_query(self, client: AsyncClient, db_session: AsyncSession) -> None:
        match = await _seed_source_document(
            db_session,
            title="other-title",
            original_filename="Hypertension_Training.mp4",
        )
        match.status = "ingested"
        match.source_type = "video"
        miss = await _seed_source_document(
            db_session,
            title="unrelated",
            original_filename="Diabetes_Overview.mp4",
        )
        miss.status = "ingested"
        miss.source_type = "video"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?source_type=video&q=hyper"))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["source_documents"]) == 1
        assert body["source_documents"][0]["original_filename"] == "Hypertension_Training.mp4"
        assert body["total_source_documents"] == 1

    async def test_filename_query_matches_title(self, client: AsyncClient, db_session: AsyncSession) -> None:
        doc = await _seed_source_document(
            db_session,
            title="BRAC Counselling Video",
            original_filename="clip-001.mp4",
        )
        doc.status = "ingested"
        doc.source_type = "video"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?source_type=video&q=counselling"))
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["source_documents"]) == 1
        assert body["source_documents"][0]["title"] == "BRAC Counselling Video"

    async def test_pagination(self, client: AsyncClient, db_session: AsyncSession) -> None:
        for i in range(3):
            doc = await _seed_source_document(db_session, title=f"doc-{i}")
            doc.status = "ingested"
            doc.ingested_at = datetime.now(UTC) + timedelta(seconds=i)
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?limit=2&offset=0"))
        body = resp.json()
        assert len(body["source_documents"]) == 2
        assert body["total_source_documents"] == 3
        assert body["total_pages"] == 2
        assert body["limit"] == 2
        assert body["offset"] == 0

        resp = await client.get(platform_path("/admin/source-documents?limit=2&offset=2"))
        body = resp.json()
        assert len(body["source_documents"]) == 1
        assert body["total_source_documents"] == 3
        assert body["total_pages"] == 2
        assert body["offset"] == 2
