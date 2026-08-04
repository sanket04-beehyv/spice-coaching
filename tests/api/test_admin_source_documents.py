"""Admin source document catalog API tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.deps import get_object_storage_client
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.conftest import _seed_source_document
from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


class TestListSourceDocuments:
    async def test_defaults_to_all_statuses(self, client: AsyncClient, db_session: AsyncSession) -> None:
        uploaded = await _seed_source_document(db_session, title="staged-doc")
        uploaded.status = "uploaded"
        ingesting = await _seed_source_document(db_session, title="in-flight-doc")
        ingesting.status = "ingesting"
        ingested = await _seed_source_document(db_session, title="ready-doc")
        ingested.status = "ingested"
        failed = await _seed_source_document(db_session, title="failed-doc")
        failed.status = "failed"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents"))
        assert resp.status_code == 200
        body = resp.json()
        rows = body["source_documents"]
        assert {row["title"] for row in rows} == {
            "staged-doc",
            "in-flight-doc",
            "ready-doc",
            "failed-doc",
        }
        assert {row["status"] for row in rows} == {"uploaded", "ingesting", "ingested", "failed"}
        assert body["total_source_documents"] == 4
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

    async def test_status_filter_uploaded(self, client: AsyncClient, db_session: AsyncSession) -> None:
        staged = await _seed_source_document(db_session, title="staged-only")
        staged.status = "uploaded"
        ingested = await _seed_source_document(db_session, title="done")
        ingested.status = "ingested"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?status=uploaded"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"staged-only"}
        assert body["total_source_documents"] == 1

    async def test_invalid_status_rejected(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/source-documents?status=unknown"))
        assert resp.status_code == 422

    async def test_invalid_status_in_multi_list_rejected(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/source-documents?status=uploaded&status=unknown"))
        assert resp.status_code == 422

    async def test_status_filter_multi_repeated(self, client: AsyncClient, db_session: AsyncSession) -> None:
        uploaded = await _seed_source_document(db_session, title="staged")
        uploaded.status = "uploaded"
        ingested = await _seed_source_document(db_session, title="done")
        ingested.status = "ingested"
        failed = await _seed_source_document(db_session, title="broke")
        failed.status = "failed"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?status=uploaded&status=ingested"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"staged", "done"}
        assert body["total_source_documents"] == 2

    async def test_status_filter_multi_comma_separated(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        uploaded = await _seed_source_document(db_session, title="staged")
        uploaded.status = "uploaded"
        ingested = await _seed_source_document(db_session, title="done")
        ingested.status = "ingested"
        failed = await _seed_source_document(db_session, title="broke")
        failed.status = "failed"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?status=uploaded,failed"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"staged", "broke"}
        assert body["total_source_documents"] == 2

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

    async def test_sort_by_validation_rejects_invalid(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/source-documents?sort_by=invalid"))
        assert resp.status_code == 422

    async def test_sort_dir_validation_rejects_invalid(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/source-documents?sort_dir=up"))
        assert resp.status_code == 422

    async def test_list_orders_by_title_asc(self, client: AsyncClient, db_session: AsyncSession) -> None:
        alpha = await _seed_source_document(db_session, title="alpha-doc")
        alpha.status = "ingested"
        beta = await _seed_source_document(db_session, title="beta-doc")
        beta.status = "ingested"
        gamma = await _seed_source_document(db_session, title="gamma-doc")
        gamma.status = "ingested"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?sort_by=title&sort_dir=asc"))
        titles = [row["title"] for row in resp.json()["source_documents"]]
        assert titles == ["alpha-doc", "beta-doc", "gamma-doc"]

    async def test_list_orders_by_ingested_at_asc(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        older = await _seed_source_document(db_session, title="older")
        older.status = "ingested"
        older.ingested_at = datetime.now(UTC) - timedelta(days=2)
        newer = await _seed_source_document(db_session, title="newer")
        newer.status = "ingested"
        newer.ingested_at = datetime.now(UTC)
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?sort_by=ingested_at&sort_dir=asc"))
        titles = [row["title"] for row in resp.json()["source_documents"]]
        assert titles == ["older", "newer"]

    async def test_list_orders_by_original_filename_nulls_last(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        with_name = await _seed_source_document(
            db_session,
            title="named",
            original_filename="alpha.pdf",
        )
        with_name.status = "ingested"
        without_name = await _seed_source_document(
            db_session,
            title="unnamed",
            original_filename=None,
        )
        without_name.status = "ingested"
        await db_session.commit()

        resp = await client.get(
            platform_path("/admin/source-documents?sort_by=original_filename&sort_dir=asc")
        )
        ids = [row["id"] for row in resp.json()["source_documents"]]
        assert ids == [str(with_name.id), str(without_name.id)]

        resp = await client.get(
            platform_path("/admin/source-documents?sort_by=original_filename&sort_dir=desc")
        )
        ids = [row["id"] for row in resp.json()["source_documents"]]
        assert ids == [str(with_name.id), str(without_name.id)]

    async def test_default_excludes_retired(self, client: AsyncClient, db_session: AsyncSession) -> None:
        active = await _seed_source_document(db_session, title="active-doc")
        active.status = "ingested"
        retired = await _seed_source_document(db_session, title="retired-doc", sync_published_visible=True)
        retired.status = "retired"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"active-doc"}
        assert body["total_source_documents"] == 1

    async def test_status_filter_retired(self, client: AsyncClient, db_session: AsyncSession) -> None:
        active = await _seed_source_document(db_session, title="active-doc")
        active.status = "ingested"
        retired = await _seed_source_document(db_session, title="retired-doc", sync_published_visible=True)
        retired.status = "retired"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?status=retired"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"retired-doc"}
        assert body["total_source_documents"] == 1

    async def test_sync_published_visible_true_excludes_retired(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        knowledge = await _seed_source_document(
            db_session, title="knowledge-active", sync_published_visible=True
        )
        knowledge.status = "uploaded"
        knowledge_retired = await _seed_source_document(
            db_session, title="knowledge-retired", sync_published_visible=True
        )
        knowledge_retired.status = "retired"
        ingest = await _seed_source_document(db_session, title="ingest-doc", sync_published_visible=False)
        ingest.status = "ingested"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?sync_published_visible=true"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"knowledge-active"}
        assert body["total_source_documents"] == 1

    async def test_sync_published_visible_false(self, client: AsyncClient, db_session: AsyncSession) -> None:
        knowledge = await _seed_source_document(
            db_session, title="knowledge-doc", sync_published_visible=True
        )
        knowledge.status = "uploaded"
        ingest = await _seed_source_document(db_session, title="ingest-doc", sync_published_visible=False)
        ingest.status = "ingested"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents?sync_published_visible=false"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"ingest-doc"}
        assert body["total_source_documents"] == 1

    async def test_omit_sync_published_visible_returns_both(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        knowledge = await _seed_source_document(
            db_session, title="knowledge-doc", sync_published_visible=True
        )
        knowledge.status = "uploaded"
        ingest = await _seed_source_document(db_session, title="ingest-doc", sync_published_visible=False)
        ingest.status = "ingested"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents"))
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"knowledge-doc", "ingest-doc"}
        assert body["total_source_documents"] == 2

    async def test_sync_published_visible_true_and_status_retired(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        knowledge_active = await _seed_source_document(
            db_session, title="knowledge-active", sync_published_visible=True
        )
        knowledge_active.status = "uploaded"
        knowledge_retired = await _seed_source_document(
            db_session, title="knowledge-retired", sync_published_visible=True
        )
        knowledge_retired.status = "retired"
        # Ingest docs cannot normally be retired via the API, but the filter
        # combination should still only return retired knowledge rows.
        ingest_retired = await _seed_source_document(
            db_session, title="ingest-retired", sync_published_visible=False
        )
        ingest_retired.status = "retired"
        await db_session.commit()

        resp = await client.get(
            platform_path("/admin/source-documents?sync_published_visible=true&status=retired")
        )
        assert resp.status_code == 200
        body = resp.json()
        assert {row["title"] for row in body["source_documents"]} == {"knowledge-retired"}
        assert body["total_source_documents"] == 1

    async def test_list_includes_stored_path(self, client: AsyncClient, db_session: AsyncSession) -> None:
        doc = await _seed_source_document(
            db_session,
            title="path-doc",
            storage_path="medtronics-storage/ingest/path-doc.pdf",
        )
        doc.status = "ingested"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents"))
        assert resp.status_code == 200
        row = resp.json()["source_documents"][0]
        assert row["stored_path"] == "medtronics-storage/ingest/path-doc.pdf"


class TestSourceDocumentMetadata:
    async def test_list_includes_description_and_thumbnail(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        doc = await _seed_source_document(db_session, title="meta-doc")
        doc.status = "ingested"
        doc.description = "A short blurb"
        doc.thumbnail_storage_path = "medtronics-storage/ingest/thumbnails/x.png"
        await db_session.commit()

        resp = await client.get(platform_path("/admin/source-documents"))
        assert resp.status_code == 200
        row = resp.json()["source_documents"][0]
        assert row["description"] == "A short blurb"
        assert row["thumbnail_storage_path"] == "medtronics-storage/ingest/thumbnails/x.png"
        assert row["stored_path"] == doc.original_storage_path

    async def test_patch_title_and_description(self, client: AsyncClient, db_session: AsyncSession) -> None:
        doc = await _seed_source_document(db_session, title="old-title")
        doc.status = "ingesting"
        await db_session.commit()

        resp = await client.patch(
            platform_path(f"/admin/source-documents/{doc.id}"),
            json={"title": "new-title", "description": "updated desc"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "new-title"
        assert body["description"] == "updated desc"
        assert body["status"] == "ingesting"

        await db_session.refresh(doc)
        assert doc.title == "new-title"
        assert doc.description == "updated desc"
        assert doc.status == "ingesting"

    async def test_patch_rejects_empty_title(self, client: AsyncClient, db_session: AsyncSession) -> None:
        doc = await _seed_source_document(db_session, title="keep")
        doc.status = "ingested"
        await db_session.commit()

        resp = await client.patch(
            platform_path(f"/admin/source-documents/{doc.id}"),
            json={"title": "   "},
        )
        assert resp.status_code == 422

    async def test_patch_does_not_create_ingestion_run(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        doc = await _seed_source_document(db_session, title="no-reingest")
        doc.status = "ingested"
        await db_session.commit()

        before = (await db_session.execute(select(func.count()).select_from(IngestionRun))).scalar_one()

        resp = await client.patch(
            platform_path(f"/admin/source-documents/{doc.id}"),
            json={"description": "still no ingest"},
        )
        assert resp.status_code == 200

        after = (await db_session.execute(select(func.count()).select_from(IngestionRun))).scalar_one()
        assert after == before

    async def test_put_thumbnail_stores_path(
        self, client: AsyncClient, db_session: AsyncSession, app
    ) -> None:
        doc = await _seed_source_document(db_session, title="thumb-doc")
        doc.status = "ingested"
        await db_session.commit()

        resp = await client.put(
            platform_path(f"/admin/source-documents/{doc.id}/thumbnail"),
            files={"file": ("thumb.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["thumbnail_storage_path"] == (f"medtronics-storage/ingest/thumbnails/{doc.id}.png")
        assert body["status"] == "ingested"

        fake = app.dependency_overrides[get_object_storage_client]()
        assert len(fake.put_calls) == 1
        assert fake.put_calls[0]["content_type"] == "image/png"

    async def test_put_thumbnail_rejects_invalid_content_type(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        doc = await _seed_source_document(db_session, title="bad-thumb")
        doc.status = "ingested"
        await db_session.commit()

        resp = await client.put(
            platform_path(f"/admin/source-documents/{doc.id}/thumbnail"),
            files={"file": ("thumb.gif", b"GIF89a", "image/gif")},
        )
        assert resp.status_code == 422
