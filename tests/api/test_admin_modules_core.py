"""Admin modules API — module CRUD, search, regenerate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import platform_service.celery_tasks as celery_tasks
import pytest
from asyncpg import Range
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.config import get_settings
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.content_block import ContentBlock
from platform_service.db.models.module import Module
from platform_service.db.models.module_behavioural_gap import ModuleBehaviouralGap
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.models.source_page import SourcePage
from platform_service.deps import get_object_storage_client
from platform_service.integrations import ai_runtime_client as arc
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.conftest import (
    _PRESIGNED_URL,
    _mock_storage,
    _seed_module,
    _seed_source_document,
    _unit_basis_vector,
)
from tests.conftest import platform_path, requires_db
from tests.localized_helpers import loc, loc_options, primary_from_response

pytestmark = [requires_db, pytest.mark.asyncio]

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


class TestListModules:
    async def test_returns_summaries(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(db_session, title_localized={"bn": "Module A"})
        await _seed_module(db_session, title_localized={"bn": "Module B"})

        resp = await client.get(platform_path("/admin/modules"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_modules"] == 2
        assert body["total_pages"] == 1
        assert body["limit"] == 50
        assert body["offset"] == 0
        data = body["modules"]
        assert len(data) == 2
        sample = data[0]
        # Summary shape: must NOT include cards or quiz (those are detail-only).
        assert "cards" not in sample
        assert "quiz" not in sample
        assert {"id", "title", "card_count", "lifecycle_status", "clinically_reviewed"} <= set(sample)

    async def test_search_metadata_null_when_unset(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _seed_module(db_session)
        resp = await client.get(platform_path("/admin/modules"))
        assert resp.json()["modules"][0]["search_metadata"] is None

    async def test_search_metadata_round_trips_when_set(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _seed_module(db_session, search_metadata_jsonb=_SAMPLE_SEARCH_METADATA)
        resp = await client.get(platform_path("/admin/modules"))
        assert resp.json()["modules"][0]["search_metadata"] == _SAMPLE_SEARCH_METADATA

    async def test_card_count_reflects_module_json(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _seed_module(
            db_session,
            module_json={
                "cards": [
                    {"title": {"bn": "1"}},
                    {"title": {"bn": "2"}},
                    {"title": {"bn": "3"}},
                ]
            },
        )
        resp = await client.get(platform_path("/admin/modules"))
        assert resp.json()["modules"][0]["card_count"] == 3

    async def test_status_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(db_session, title_localized={"bn": "live"})
        await _seed_module(
            db_session, title_localized={"bn": "gone"}, lifecycle_status="retired", set_family_pointer=False
        )

        # Default excludes retired.
        default_resp = await client.get(platform_path("/admin/modules"))
        titles = {primary_from_response(m) for m in default_resp.json()["modules"]}
        assert "live" in titles and "gone" not in titles

        # Explicit retired filter shows only retired.
        retired_resp = await client.get(platform_path("/admin/modules?status=retired"))
        titles = {primary_from_response(m) for m in retired_resp.json()["modules"]}
        assert titles == {"gone"}

    async def test_clinically_reviewed_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(db_session, title_localized={"bn": "reviewed"}, clinically_reviewed=True)
        await _seed_module(db_session, title_localized={"bn": "pending"}, clinically_reviewed=False)

        resp = await client.get(platform_path("/admin/modules?clinically_reviewed=true"))
        titles = {primary_from_response(m) for m in resp.json()["modules"]}
        assert titles == {"reviewed"}

        resp = await client.get(platform_path("/admin/modules?clinically_reviewed=false"))
        titles = {primary_from_response(m) for m in resp.json()["modules"]}
        assert titles == {"pending"}

    async def test_full_text_query_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(db_session, title_localized={"bn": "Pregnancy referral"})
        await _seed_module(db_session, title_localized={"bn": "Diabetes screening"})

        resp = await client.get(platform_path("/admin/modules?q=referral"))
        titles = {primary_from_response(m) for m in resp.json()["modules"]}
        assert titles == {"Pregnancy referral"}

    async def test_has_visibility_window_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        await _seed_module(
            db_session,
            title_localized={"bn": "windowed"},
            visibility_window=Range(now, now + timedelta(days=7), lower_inc=True, upper_inc=False),
        )
        await _seed_module(db_session, title_localized={"bn": "no-window"})

        resp = await client.get(platform_path("/admin/modules?has_visibility_window=true"))
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"windowed"}

        resp = await client.get(platform_path("/admin/modules?has_visibility_window=false"))
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"no-window"}

    async def test_pagination_limit_offset(self, client: AsyncClient, db_session: AsyncSession) -> None:
        for i in range(5):
            await _seed_module(db_session, title_localized={"bn": f"m{i}"})

        resp = await client.get(platform_path("/admin/modules?limit=2&offset=0"))
        body = resp.json()
        assert len(body["modules"]) == 2
        assert body["total_modules"] == 5
        assert body["total_pages"] == 3
        assert body["limit"] == 2
        assert body["offset"] == 0

        resp = await client.get(platform_path("/admin/modules?limit=2&offset=2"))
        body = resp.json()
        assert len(body["modules"]) == 2
        assert body["total_modules"] == 5
        assert body["offset"] == 2

        resp = await client.get(platform_path("/admin/modules?limit=2&offset=4"))
        body = resp.json()
        assert len(body["modules"]) == 1
        assert body["total_modules"] == 5

    async def test_limit_validation_rejects_zero(self, client: AsyncClient, db_session: AsyncSession) -> None:
        resp = await client.get(platform_path("/admin/modules?limit=0"))
        assert resp.status_code == 422

    async def test_limit_validation_rejects_excessive(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        resp = await client.get(platform_path("/admin/modules?limit=500"))
        assert resp.status_code == 422

    async def test_quality_flags_surfaced_in_summary(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        flags = {"flags": ["insufficient_tokens"]}
        await _seed_module(db_session, title_localized={"bn": "flagged"}, quality_flags_jsonb=flags)
        await _seed_module(db_session, title_localized={"bn": "clean"}, quality_flags_jsonb=None)

        resp = await client.get(platform_path("/admin/modules"))
        rows = {primary_from_response(m): m for m in resp.json()["modules"]}
        assert rows["flagged"]["quality_flags"] == flags
        assert rows["clean"]["quality_flags"] is None

    async def test_has_quality_flags_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(
            db_session,
            title_localized={"bn": "flagged"},
            quality_flags_jsonb={"flags": ["insufficient_tokens"]},
        )
        await _seed_module(db_session, title_localized={"bn": "clean-null"}, quality_flags_jsonb=None)
        # Empty dict should be treated as "no flags".
        await _seed_module(db_session, title_localized={"bn": "clean-empty"}, quality_flags_jsonb={})

        resp = await client.get(platform_path("/admin/modules?has_quality_flags=true"))
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"flagged"}

        resp = await client.get(platform_path("/admin/modules?has_quality_flags=false"))
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"clean-null", "clean-empty"}

    async def test_domain_topic_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(db_session, title_localized=loc("rmnch-mod"), domain="rmnch")
        await _seed_module(db_session, title_localized=loc("ncd-mod"), domain="ncd")

        resp = await client.get(platform_path("/admin/modules?domain=ncd"))
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"ncd-mod"}

    async def test_source_document_ids_in_list_response(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        doc = await _seed_source_document(db_session)
        await _seed_module(
            db_session,
            title_localized=loc("linked-draft"),
            lifecycle_status="draft",
            source_document_ids=[doc.id],
            set_family_pointer=False,
        )

        resp = await client.get(platform_path("/admin/modules?status=draft"))
        assert resp.status_code == 200
        rows = {primary_from_response(m): m for m in resp.json()["modules"]}
        assert set(rows["linked-draft"]["source_document_ids"]) == {str(doc.id)}

    async def test_source_document_id_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        doc_a = await _seed_source_document(db_session, title="doc-a")
        doc_b = await _seed_source_document(db_session, title="doc-b")
        await _seed_module(
            db_session,
            title_localized=loc("from-doc-a"),
            lifecycle_status="draft",
            source_document_ids=[doc_a.id],
            set_family_pointer=False,
        )
        await _seed_module(
            db_session,
            title_localized=loc("from-doc-b"),
            lifecycle_status="draft",
            source_document_ids=[doc_b.id],
            set_family_pointer=False,
        )

        resp = await client.get(platform_path(f"/admin/modules?status=draft&source_document_id={doc_a.id}"))
        assert resp.status_code == 200
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"from-doc-a"}

    async def test_list_module_domains(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(
            db_session,
            title_localized=loc("published-rmnch"),
            domain="rmnch",
            lifecycle_status="published",
        )
        await _seed_module(
            db_session,
            title_localized=loc("draft-ncd"),
            domain="ncd",
            lifecycle_status="draft",
        )
        await _seed_module(
            db_session,
            title_localized=loc("retired-clinical"),
            domain="clinical",
            lifecycle_status="retired",
        )

        resp = await client.get(platform_path("/admin/modules/domains"))
        assert resp.status_code == 200
        assert resp.json() == ["ncd", "rmnch"]

        resp = await client.get(platform_path("/admin/modules/domains?status=published"))
        assert resp.json() == ["rmnch"]

        resp = await client.get(platform_path("/admin/modules/domains?status=draft"))
        assert resp.json() == ["ncd"]

    async def test_published_date_range_filters_published_at(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        inside = datetime(2025, 3, 15, 12, 0, tzinfo=UTC)
        outside = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
        await _seed_module(
            db_session,
            title_localized=loc("in-range"),
            lifecycle_status="published",
            published_at=inside,
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        await _seed_module(
            db_session,
            title_localized=loc("out-of-range"),
            lifecycle_status="published",
            published_at=outside,
            created_at=datetime(2020, 1, 1, tzinfo=UTC),
        )

        resp = await client.get(
            platform_path(
                "/admin/modules?status=published"
                "&published_from=2025-03-01T00:00:00Z&published_to=2025-03-31T23:59:59Z"
            )
        )
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"in-range"}

    async def test_created_date_range_filters_created_at(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        inside = datetime(2025, 6, 10, 8, 0, tzinfo=UTC)
        outside = datetime(2023, 6, 10, 8, 0, tzinfo=UTC)
        await _seed_module(
            db_session,
            title_localized=loc("recent-draft"),
            lifecycle_status="draft",
            created_at=inside,
            published_at=None,
            set_family_pointer=False,
        )
        await _seed_module(
            db_session,
            title_localized=loc("old-draft"),
            lifecycle_status="draft",
            created_at=outside,
            published_at=None,
            set_family_pointer=False,
        )

        resp = await client.get(
            platform_path(
                "/admin/modules?status=draft"
                "&created_from=2025-06-01T00:00:00Z&created_to=2025-06-30T23:59:59Z"
            )
        )
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"recent-draft"}

    async def test_combined_domain_and_typed_date_filters(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        when = datetime(2025, 2, 1, 12, 0, tzinfo=UTC)
        await _seed_module(
            db_session,
            title_localized=loc("match"),
            domain="rmnch",
            lifecycle_status="published",
            published_at=when,
        )
        await _seed_module(
            db_session,
            title_localized=loc("wrong-topic"),
            domain="ncd",
            lifecycle_status="published",
            published_at=when,
        )
        await _seed_module(
            db_session,
            title_localized=loc("wrong-date"),
            domain="rmnch",
            lifecycle_status="published",
            published_at=datetime(2024, 2, 1, 12, 0, tzinfo=UTC),
        )

        resp = await client.get(
            platform_path(
                "/admin/modules?status=published&domain=rmnch"
                "&published_from=2025-01-01T00:00:00Z"
                "&published_to=2025-12-31T23:59:59Z"
            )
        )
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"match"}

    async def test_multi_typed_date_filters_are_anded(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _seed_module(
            db_session,
            title_localized=loc("both"),
            lifecycle_status="published",
            created_at=datetime(2025, 1, 10, tzinfo=UTC),
            published_at=datetime(2025, 2, 10, tzinfo=UTC),
        )
        await _seed_module(
            db_session,
            title_localized=loc("created-only"),
            lifecycle_status="published",
            created_at=datetime(2025, 1, 10, tzinfo=UTC),
            published_at=datetime(2024, 2, 10, tzinfo=UTC),
        )

        resp = await client.get(
            platform_path(
                "/admin/modules?status=published"
                "&created_from=2025-01-01T00:00:00Z&created_to=2025-01-31T23:59:59Z"
                "&published_from=2025-02-01T00:00:00Z&published_to=2025-02-28T23:59:59Z"
            )
        )
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"both"}

    async def test_activated_date_uses_coalesce(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(
            db_session,
            title_localized=loc("reactivated"),
            lifecycle_status="published",
            published_at=datetime(2024, 1, 1, tzinfo=UTC),
            created_at=datetime(2023, 1, 1, tzinfo=UTC),
        )
        m.first_activated_at = datetime(2024, 6, 1, tzinfo=UTC)
        m.last_reactivated_at = datetime(2025, 3, 15, tzinfo=UTC)
        await db_session.commit()

        outside = await _seed_module(
            db_session,
            title_localized=loc("old-activation"),
            lifecycle_status="published",
            published_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        outside.first_activated_at = datetime(2024, 1, 15, tzinfo=UTC)
        await db_session.commit()

        resp = await client.get(
            platform_path(
                "/admin/modules?status=published"
                "&activated_from=2025-03-01T00:00:00Z&activated_to=2025-03-31T23:59:59Z"
            )
        )
        assert {primary_from_response(m) for m in resp.json()["modules"]} == {"reactivated"}

    async def test_invalid_typed_date_range_returns_422(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        resp = await client.get(
            platform_path("/admin/modules?created_from=2025-12-31T00:00:00Z&created_to=2025-01-01T00:00:00Z")
        )
        assert resp.status_code == 422
        assert "created_from" in resp.json()["detail"]


class TestGetModuleDetail:
    async def test_returns_full_payload(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(
            db_session,
            module_json={"cards": [{"title": {"bn": "C1"}, "body": {"bn": "B1"}}]},
        )

        resp = await client.get(platform_path(f"/admin/modules/{m.id}"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(m.id)
        assert data["title"]["bn"] == "Sample"
        assert isinstance(data["cards"], list)
        assert isinstance(data["quiz"], list)
        assert data["cards"][0]["title"]["bn"] == "C1"
        assert data["cards"][0]["source_pages"] == []
        # Detail-only fields are present.
        assert "difficulty_level" in data
        assert "pass_threshold_override" in data
        assert data["source_documents"] == []

    async def test_search_metadata_round_trips_when_set(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        m = await _seed_module(db_session, search_metadata_jsonb=_SAMPLE_SEARCH_METADATA)
        resp = await client.get(platform_path(f"/admin/modules/{m.id}"))
        assert resp.status_code == 200
        assert resp.json()["search_metadata"] == _SAMPLE_SEARCH_METADATA

    async def test_cards_include_source_pages_from_block_ids(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        doc = await _seed_source_document(db_session)
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

        m = await _seed_module(
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
        await db_session.commit()

        resp = await client.get(platform_path(f"/admin/modules/{m.id}"))
        assert resp.status_code == 200
        card = resp.json()["cards"][0]
        page_ref = card["source_pages"][0]
        assert page_ref["source_document_id"] == str(doc.id)
        assert page_ref["page_number"] == 12
        assert page_ref["start_ms"] is None
        assert page_ref["end_ms"] is None
        assert page_ref["presigned_url"].endswith("#page=12")
        assert "source-documents/abc_manual.pdf" in page_ref["presigned_url"]
        assert page_ref["presigned_expires_seconds"] == get_settings().admin_file_presigned_max_seconds

    async def test_source_documents_empty_when_no_linked_docs(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        m = await _seed_module(db_session, source_document_ids=None)
        resp = await client.get(platform_path(f"/admin/modules/{m.id}"))
        assert resp.status_code == 200
        assert resp.json()["source_documents"] == []

    async def test_source_documents_presigned_url(self, app: FastAPI, db_session: AsyncSession) -> None:
        doc = await _seed_source_document(db_session)
        m = await _seed_module(db_session, source_document_ids=[doc.id])
        mock_storage = _mock_storage()
        app.dependency_overrides[get_object_storage_client] = lambda: mock_storage

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(platform_path(f"/admin/modules/{m.id}"))

        assert resp.status_code == 200
        sources = resp.json()["source_documents"]
        assert len(sources) == 1
        assert sources[0]["source_document_id"] == str(doc.id)
        assert sources[0]["presigned_url"] == _PRESIGNED_URL
        assert sources[0]["presigned_expires_seconds"] == get_settings().admin_file_presigned_max_seconds
        mock_storage.presigned_get_url.assert_awaited_once()

    async def test_source_documents_null_presigned_on_legacy_path(
        self, app: FastAPI, db_session: AsyncSession
    ) -> None:
        doc = await _seed_source_document(db_session, storage_path="/var/legacy/manual.pdf")
        m = await _seed_module(db_session, source_document_ids=[doc.id])
        mock_storage = _mock_storage()
        app.dependency_overrides[get_object_storage_client] = lambda: mock_storage

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(platform_path(f"/admin/modules/{m.id}"))

        assert resp.status_code == 200
        sources = resp.json()["source_documents"]
        assert len(sources) == 1
        assert sources[0]["source_document_id"] == str(doc.id)
        assert sources[0]["presigned_url"] is None
        assert sources[0]["presigned_expires_seconds"] is None
        mock_storage.presigned_get_url.assert_not_awaited()

    async def test_quiz_join_orders_by_question_order(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        m = await _seed_module(db_session)
        # Insert in reverse order; response should be 1, 2, 3.
        for order in (3, 1, 2):
            db_session.add(
                ModuleQuizQuestion(
                    module_id=m.id,
                    question_order=order,
                    question_family_id=uuid4(),
                    question_version=1,
                    question_localized={"bn": f"Q{order}"},
                    options_localized={"bn": ["a", "b", "c", "d"]},
                    correct_indices=[0],
                )
            )
        await db_session.commit()

        resp = await client.get(platform_path(f"/admin/modules/{m.id}"))
        quiz = resp.json()["quiz"]
        assert [q["question_order"] for q in quiz] == [1, 2, 3]

    async def test_returns_404_for_missing(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path(f"/admin/modules/{uuid4()}"))
        assert resp.status_code == 404

    async def test_visibility_window_serialised_with_lower_upper(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        starts = datetime(2026, 6, 1, tzinfo=UTC)
        ends = datetime(2026, 6, 30, tzinfo=UTC)
        m = await _seed_module(
            db_session,
            visibility_window=Range(starts, ends, lower_inc=True, upper_inc=False),
        )
        resp = await client.get(platform_path(f"/admin/modules/{m.id}"))
        data = resp.json()
        # ISO 8601 with Z or +00:00 — accept both.
        lower = data["visibility_window_lower"]
        upper = data["visibility_window_upper"]
        assert lower is not None
        assert upper is not None
        assert "2026-06-01" in lower
        assert "2026-06-30" in upper

    async def test_null_visibility_window_serialises_as_null(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        m = await _seed_module(db_session, visibility_window=None)
        resp = await client.get(platform_path(f"/admin/modules/{m.id}"))
        data = resp.json()
        assert data["visibility_window_lower"] is None
        assert data["visibility_window_upper"] is None
        assert data["has_visibility_window"] is False


# ─── POST /admin/modules ───────────────────────────────────────────────────


class TestCreateModule:
    async def test_creates_manual_module_with_quiz(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        resp = await client.post(
            platform_path("/admin/modules"),
            json={
                "title": loc("নতুন মডিউল"),
                "description": loc("Manual desc"),
                "domain": "clinical",
                "module_json": {
                    "cards": [
                        {
                            "title": loc("C1"),
                            "body": loc("B1"),
                        }
                    ]
                },
                "quiz": [
                    {
                        "question": loc("Q1 Bangla"),
                        "options": loc_options(["A", "B"]),
                        "correct_indices": [0],
                    }
                ],
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body
        assert "module_family_id" in body
        assert body["version"] == 1

        new_id = UUID(body["id"])

        # Verify created entities in DB
        # Module family
        family = (
            await db_session.execute(
                select(ModuleFamily).where(ModuleFamily.id == UUID(body["module_family_id"]))
            )
        ).scalar_one_or_none()
        assert family is not None
        assert family.module_code == "নতন-মডউল"

        # Module
        module = (await db_session.execute(select(Module).where(Module.id == new_id))).scalar_one_or_none()
        assert module is not None
        assert module.title_localized["bn"] == "নতুন মডিউল"
        assert "en" not in module.title_localized
        assert module.lifecycle_status == "draft"
        assert module.clinically_reviewed is False

        # Quiz question
        questions = (
            (
                await db_session.execute(
                    select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == new_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(questions) == 1
        assert questions[0].question_localized["bn"] == "Q1 Bangla"
        assert questions[0].question_version == 1

        # Primary gap auto-creation
        assert module.primary_gap_id is not None
        gap = (
            await db_session.execute(select(BehaviouralGap).where(BehaviouralGap.id == module.primary_gap_id))
        ).scalar_one_or_none()
        assert gap is not None
        assert gap.description == "নতুন মডিউল"
        assert gap.gap_code.startswith("module_primary_gap_")

        # Gap link
        link = (
            await db_session.execute(
                select(ModuleBehaviouralGap).where(
                    ModuleBehaviouralGap.module_id == new_id,
                    ModuleBehaviouralGap.behavioural_gap_id == gap.id,
                )
            )
        ).scalar_one_or_none()
        assert link is not None
        assert link.is_primary is True

    async def test_creates_manual_module_uses_provided_gaps(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        # Pre-seed a behavioral gap
        gap = BehaviouralGap(
            gap_code="custom_gap_code_123",
            description="Custom Gap",
            domain="clinical",
            severity_default="moderate",
            detection_rule_jsonb={},
            status="active",
        )
        db_session.add(gap)
        await db_session.flush()
        await db_session.commit()

        resp = await client.post(
            platform_path("/admin/modules"),
            json={
                "title": loc("নতুন মডিউল ২"),
                "domain": "clinical",
                "behavioural_gap_ids": [str(gap.id)],
                "primary_gap_id": str(gap.id),
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        new_id = UUID(body["id"])

        module = (await db_session.execute(select(Module).where(Module.id == new_id))).scalar_one_or_none()
        assert module is not None
        assert module.primary_gap_id == gap.id

    async def test_creates_chatbot_faqs_only_module_without_gap(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        resp = await client.post(
            platform_path("/admin/modules"),
            json={
                "title": loc("চ্যাটবট FAQ"),
                "domain": "clinical",
                "chatbot_faqs_only": True,
                "module_json": {"cards": [{"title": loc("FAQ card"), "body": loc("FAQ body")}]},
            },
        )
        assert resp.status_code == 201
        body = resp.json()

        module = await db_session.get(Module, UUID(body["id"]))
        assert module is not None
        assert module.chatbot_faqs_only is True
        assert module.primary_gap_id is None

        detail = await client.get(platform_path(f"/admin/modules/{body['id']}"))
        assert detail.status_code == 200
        assert detail.json()["chatbot_faqs_only"] is True

    async def test_rejects_gap_links_on_chatbot_faqs_only_create(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        gap = BehaviouralGap(
            gap_code="faq_gap_conflict",
            description="Gap",
            domain="clinical",
            severity_default="moderate",
            detection_rule_jsonb={},
            status="active",
        )
        db_session.add(gap)
        await db_session.commit()

        resp = await client.post(
            platform_path("/admin/modules"),
            json={
                "title": loc("FAQ conflict"),
                "chatbot_faqs_only": True,
                "behavioural_gap_ids": [str(gap.id)],
            },
        )
        assert resp.status_code == 400


# ─── PUT /admin/modules/{id} ───────────────────────────────────────────────


class TestEditModule:
    async def test_creates_new_version(self, client: AsyncClient, db_session: AsyncSession) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "title": loc("v2 title")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["module_family_id"] == str(v1.module_family_id)
        assert body["version"] == 2
        assert body["supersedes_module_id"] == str(v1.id)
        # v2 has its own module id, distinct from v1.
        assert body["id"] != str(v1.id)

    async def test_identical_complete_snapshot_is_noop(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(
            db_session,
            title_localized={"bn": "stable title"},
            description_localized={"bn": "stable desc"},
            clinically_reviewed=True,
        )

        detail_resp = await client.get(platform_path(f"/admin/modules/{v1.id}"))
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        snapshot = {
            "expected_version": detail["version"],
            "title": detail["title"],
            "description": detail["description"],
            "module_json": {
                "cards": [{k: v for k, v in card.items() if k != "source_pages"} for card in detail["cards"]],
                "attachments": detail.get("attachments") or [],
                "quiz": detail.get("quiz") or [],
            },
            "thumbnail_storage_path": detail.get("thumbnail_storage_path"),
        }

        first = await client.put(platform_path(f"/admin/modules/{v1.id}"), json=snapshot)
        assert first.status_code == 200
        tip_id = first.json()["id"]
        tip_version = first.json()["version"]

        tip_detail = (await client.get(platform_path(f"/admin/modules/{tip_id}"))).json()
        tip_snapshot = {
            "expected_version": tip_detail["version"],
            "title": tip_detail["title"],
            "description": tip_detail["description"],
            "module_json": {
                "cards": [
                    {k: v for k, v in card.items() if k != "source_pages"} for card in tip_detail["cards"]
                ],
                "attachments": tip_detail.get("attachments") or [],
                "quiz": tip_detail.get("quiz") or [],
            },
            "thumbnail_storage_path": tip_detail.get("thumbnail_storage_path"),
        }

        second = await client.put(platform_path(f"/admin/modules/{tip_id}"), json=tip_snapshot)
        assert second.status_code == 200
        assert second.json() == first.json()

        tip_again = (await db_session.execute(select(Module).where(Module.id == tip_id))).scalar_one()
        assert tip_again.version == tip_version
        max_version = (
            await db_session.execute(
                select(func.max(Module.version)).where(Module.module_family_id == v1.module_family_id)
            )
        ).scalar_one()
        assert max_version == tip_version

    async def test_fe_shaped_complete_snapshot_with_nested_quiz_is_noop(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """Dashboard sends quiz inside module_json and omits chatbot_faqs_only."""
        v1 = await _seed_module(
            db_session,
            title_localized={"bn": "fe title"},
            description_localized={"bn": "fe desc"},
        )
        detail = (await client.get(platform_path(f"/admin/modules/{v1.id}"))).json()
        snapshot = {
            "expected_version": detail["version"],
            "title": detail["title"],
            "description": detail["description"],
            "module_json": {
                "cards": [{k: v for k, v in card.items() if k != "source_pages"} for card in detail["cards"]],
                "quiz": detail.get("quiz") or [],
            },
            "thumbnail_storage_path": detail.get("thumbnail_storage_path"),
        }
        resp = await client.put(platform_path(f"/admin/modules/{v1.id}"), json=snapshot)
        assert resp.status_code == 200
        assert resp.json()["id"] != str(v1.id)
        assert resp.json()["version"] == v1.version + 1

    async def test_complete_snapshot_with_change_creates_version(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(
            db_session,
            title_localized={"bn": "before"},
            description_localized={"bn": "desc"},
        )
        detail = (await client.get(platform_path(f"/admin/modules/{v1.id}"))).json()
        snapshot = {
            "expected_version": detail["version"],
            "title": loc("after"),
            "description": detail["description"],
            "module_json": {
                "cards": detail["cards"],
                "attachments": detail.get("attachments") or [],
                "quiz": detail.get("quiz") or [],
            },
            "thumbnail_storage_path": detail.get("thumbnail_storage_path"),
        }
        resp = await client.put(platform_path(f"/admin/modules/{v1.id}"), json=snapshot)
        assert resp.status_code == 200
        assert resp.json()["id"] != str(v1.id)
        assert resp.json()["version"] == 2

    async def test_omitted_content_field_still_creates_version(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})
        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "title": loc("v1 title")},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] != str(v1.id)
        assert resp.json()["version"] == 2

    async def test_stale_version_with_identical_content_still_conflicts(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(
            db_session,
            title_localized={"bn": "stable"},
            description_localized={"bn": "desc"},
        )
        detail = (await client.get(platform_path(f"/admin/modules/{v1.id}"))).json()
        snapshot = {
            "expected_version": detail["version"],
            "title": detail["title"],
            "description": detail["description"],
            "module_json": {
                "cards": detail["cards"],
                "attachments": detail.get("attachments") or [],
                "quiz": detail.get("quiz") or [],
            },
            "thumbnail_storage_path": detail.get("thumbnail_storage_path"),
        }
        bumped = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={
                **snapshot,
                "title": loc("changed"),
            },
        )
        assert bumped.status_code == 200
        v2_id = bumped.json()["id"]

        stale = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={**snapshot, "expected_version": v1.version},
        )
        assert stale.status_code == 409
        detail_body = stale.json()["detail"]
        assert detail_body["code"] == "module_version_conflict"
        assert detail_body["latest_module_id"] == v2_id

    async def test_rejects_missing_expected_version(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"title": loc("v2 title")},
        )
        assert resp.status_code == 422

    async def test_rejects_wrong_expected_version(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version + 5, "title": loc("stale")},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "module_version_conflict"
        assert detail["expected_version"] == v1.version + 5
        assert detail["current_version"] == v1.version
        assert detail["latest_module_id"] == str(v1.id)

    async def test_rejects_edit_of_superseded_tip(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})

        first = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "title": loc("v2 title")},
        )
        assert first.status_code == 200
        v2_id = first.json()["id"]
        assert first.json()["version"] == 2

        stale = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "title": loc("fork")},
        )
        assert stale.status_code == 409
        detail = stale.json()["detail"]
        assert detail["code"] == "module_version_conflict"
        assert detail["expected_version"] == v1.version
        assert detail["current_version"] == 2
        assert detail["latest_module_id"] == v2_id

    async def test_updates_chatbot_faqs_only_on_module(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "training module"})

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "chatbot_faqs_only": True},
        )
        assert resp.status_code == 200

        v2 = await db_session.get(Module, UUID(resp.json()["id"]))
        assert v2 is not None
        assert v2.chatbot_faqs_only is True

    async def test_rejects_gap_links_when_current_module_is_chatbot_faqs_only(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        gap = BehaviouralGap(
            gap_code="faq_edit_gap_conflict",
            description="Gap",
            domain="clinical",
            severity_default="moderate",
            detection_rule_jsonb={},
            status="active",
        )
        db_session.add(gap)
        await db_session.flush()
        v1 = await _seed_module(
            db_session,
            title_localized={"bn": "faq only module"},
            chatbot_faqs_only=True,
            primary_gap_id=None,
        )
        await db_session.commit()

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "behavioural_gap_ids": [str(gap.id)]},
        )
        assert resp.status_code == 400

    async def test_adds_quiz_questions(self, client: AsyncClient, db_session: AsyncSession) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={
                "expected_version": v1.version,
                "title": loc("v2 title"),
                "quiz": [
                    {
                        "question": loc("Q1 Bangla"),
                        "options": loc_options(["A", "B", "C", "D"]),
                        "correct_indices": [0],
                    },
                    {
                        "question": loc("Q2 Bangla"),
                        "options": loc_options(["W", "X", "Y", "Z"]),
                        "correct_indices": [1],
                    },
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        new_module_id = body["id"]

        # Verify questions were created
        # Need to use a fresh session or refresh to see the changes committed by the endpoint
        # Wait, the endpoint commits, so we should be able to see it in db_session if we don't use cache.
        # Let's use a select statement.
        stmt = select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == UUID(new_module_id))
        result = await db_session.execute(stmt)
        questions = result.scalars().all()
        assert len(questions) == 2
        # Order might be by id or question_order. We didn't specify question_order in request,
        # so it defaults to index (1, 2).
        # Let's check by primary locale question text to be safe.
        q_bns = {q.question_localized["bn"] for q in questions}
        assert q_bns == {"Q1 Bangla", "Q2 Bangla"}

    async def test_updates_existing_quiz_question(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})

        # Create a question for v1
        q1 = ModuleQuizQuestion(
            module_id=v1.id,
            question_order=1,
            question_family_id=uuid4(),
            question_version=1,
            question_localized={"bn": "Q1 Original"},
            options_localized={"bn": ["A", "B", "C", "D"]},
            correct_indices=[0],
        )
        db_session.add(q1)
        await db_session.commit()

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={
                "expected_version": v1.version,
                "title": loc("v2 title"),
                "quiz": [
                    {
                        "id": str(q1.id),
                        "question": loc("Q1 Updated"),
                        "options": loc_options(["A", "B", "C", "D"]),
                        "correct_indices": [0],
                    },
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        new_module_id = body["id"]

        # Verify question was updated (new row with same family, incremented version)

        stmt = select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == UUID(new_module_id))
        result = await db_session.execute(stmt)
        questions = result.scalars().all()
        assert len(questions) == 1
        assert questions[0].question_localized["bn"] == "Q1 Updated"
        assert questions[0].question_family_id == q1.question_family_id
        assert questions[0].question_version == 2

    async def test_adds_quiz_questions_from_module_json(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={
                "expected_version": v1.version,
                "title": loc("v2 title"),
                "module_json": {
                    "cards": [],
                    "quiz": [
                        {
                            "question": loc("Test question from json"),
                            "options": loc_options(["A"]),
                            "correct_indices": [0],
                        }
                    ],
                },
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        new_module_id = body["id"]

        # Verify questions were created
        stmt = select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == UUID(new_module_id))
        result = await db_session.execute(stmt)
        questions = result.scalars().all()
        assert len(questions) == 1
        assert questions[0].question_localized["bn"] == "Test question from json"
        assert "en" not in questions[0].question_localized

    async def test_returns_404_for_unknown(self, client: AsyncClient) -> None:
        resp = await client.put(
            platform_path(f"/admin/modules/{uuid4()}"),
            json={"expected_version": 1, "title": loc("noop")},
        )
        assert resp.status_code == 404

    async def test_returns_404_for_retired(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(db_session, lifecycle_status="retired", set_family_pointer=False)
        resp = await client.put(
            platform_path(f"/admin/modules/{m.id}"),
            json={"expected_version": m.version, "title": loc("won't take")},
        )
        assert resp.status_code == 404

    async def test_put_persists_file_and_youtube_attachments(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        card_family_id = str(uuid4())
        file_aid = str(uuid4())
        yt_aid = str(uuid4())
        v1 = await _seed_module(
            db_session,
            module_json={
                "cards": [
                    {
                        "title": loc("Card"),
                        "body": loc("Body"),
                        "card_family_id": card_family_id,
                    }
                ]
            },
        )
        module_json = {
            "attachments": [
                {
                    "kind": "file",
                    "attachment_id": file_aid,
                    "storage_path": "medtronics-storage/media/guide.pdf",
                    "object_name": "media/guide.pdf",
                    "content_type": "application/pdf",
                    "media_kind": "pdf",
                },
                {
                    "kind": "youtube",
                    "attachment_id": yt_aid,
                    "youtube_url": "https://youtu.be/dQw4w9WgXcQ",
                },
            ],
            "cards": [
                {
                    "title": loc("Card"),
                    "body": loc("Body"),
                    "card_family_id": card_family_id,
                    "attachments": [
                        {
                            "kind": "file",
                            "attachment_id": str(uuid4()),
                            "storage_path": "medtronics-storage/media/diagram.png",
                            "object_name": "media/diagram.png",
                            "content_type": "image/png",
                            "media_kind": "image",
                        }
                    ],
                }
            ],
        }
        put_resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "module_json": module_json},
        )
        assert put_resp.status_code == 200
        new_id = put_resp.json()["id"]

        get_resp = await client.get(platform_path(f"/admin/modules/{new_id}"))
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert len(data["attachments"]) == 2
        file_att = next(a for a in data["attachments"] if a["kind"] == "file")
        assert file_att["object_name"] == "media/guide.pdf"
        assert "presigned_url" not in file_att
        yt_att = next(a for a in data["attachments"] if a["kind"] == "youtube")
        assert yt_att["youtube_video_id"] == "dQw4w9WgXcQ"
        card = data["cards"][0]
        assert len(card["attachments"]) == 1
        assert card["attachments"][0]["object_name"] == "media/diagram.png"
        assert "presigned_url" not in card["attachments"][0]

    async def test_put_round_trips_prosemirror_card_body(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session)
        rich_body = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Rich body text"}],
                }
            ],
        }
        module_json = {
            "cards": [
                {
                    "title": loc("কার্ড"),
                    "body": {"bn": rich_body, "en": rich_body},
                }
            ]
        }
        put_resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "module_json": module_json},
        )
        assert put_resp.status_code == 200
        new_id = put_resp.json()["id"]

        get_resp = await client.get(platform_path(f"/admin/modules/{new_id}"))
        assert get_resp.status_code == 200
        card = get_resp.json()["cards"][0]
        assert card["body"]["bn"] == rich_body
        assert card["body"]["en"] == rich_body

    async def test_put_round_trips_block_list_card_body(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session)
        rich_body = [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "Rich block list body",
                        "marks": [{"type": "bold"}],
                    }
                ],
            }
        ]
        module_json = {
            "cards": [
                {
                    "title": loc("কার্ড"),
                    "body": {"bn": rich_body, "en": rich_body},
                }
            ]
        }
        put_resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "module_json": module_json},
        )
        assert put_resp.status_code == 200
        new_id = put_resp.json()["id"]

        get_resp = await client.get(platform_path(f"/admin/modules/{new_id}"))
        assert get_resp.status_code == 200
        card = get_resp.json()["cards"][0]
        assert card["body"]["bn"] == rich_body
        assert card["body"]["en"] == rich_body

    async def test_put_rejects_invalid_card_body_shape(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session)
        module_json = {"cards": [{"title": loc("কার্ড"), "body": {"bn": {"foo": 1}}}]}
        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "module_json": module_json},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "invalid_card_body"

    async def test_put_rejects_invalid_attachment_prefix(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        v1 = await _seed_module(db_session)
        module_json = {
            "attachments": [
                {
                    "kind": "file",
                    "attachment_id": str(uuid4()),
                    "storage_path": "medtronics-storage/evil/x.pdf",
                    "object_name": "evil/x.pdf",
                    "content_type": "application/pdf",
                    "media_kind": "pdf",
                }
            ],
            "cards": [],
        }
        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"expected_version": v1.version, "module_json": module_json},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "invalid_attachment_object_prefix"


# ─── POST /admin/modules/{id}/clinically-reviewed ──────────────────────────


class TestClinicallyReviewedEndpoint:
    async def test_flips_flag_to_true_with_audit(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(db_session, clinically_reviewed=False)
        reviewer = uuid4()

        resp = await client.post(
            platform_path(f"/admin/modules/{m.id}/clinically-reviewed"),
            json={"clinically_reviewed": True, "reviewer_id": str(reviewer)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["clinically_reviewed"] is True
        assert data["clinically_reviewed_at"] is not None
        assert data["clinically_reviewed_by"] == str(reviewer)

    async def test_flips_flag_to_false_clears_audit(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        m = await _seed_module(db_session, clinically_reviewed=True)
        # Pre-populate the audit fields.
        m.clinically_reviewed_at = datetime.now(UTC)
        m.clinically_reviewed_by = uuid4()
        await db_session.commit()

        resp = await client.post(
            platform_path(f"/admin/modules/{m.id}/clinically-reviewed"),
            json={"clinically_reviewed": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["clinically_reviewed"] is False
        assert data["clinically_reviewed_at"] is None
        assert data["clinically_reviewed_by"] is None

    async def test_returns_404_for_unknown(self, client: AsyncClient) -> None:
        resp = await client.post(
            platform_path(f"/admin/modules/{uuid4()}/clinically-reviewed"),
            json={"clinically_reviewed": True},
        )
        assert resp.status_code == 404


# ─── POST /admin/modules/{id}/visibility-window ────────────────────────────


class TestVisibilityWindowEndpoint:
    async def test_set_window_with_iso_timestamps(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        m = await _seed_module(db_session)

        resp = await client.post(
            platform_path(f"/admin/modules/{m.id}/visibility-window"),
            json={
                "starts_at": "2026-06-01T00:00:00Z",
                "ends_at": "2026-06-30T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["visibility_window"] is not None
        assert "2026-06-01" in data["visibility_window"]["lower"]
        assert "2026-06-30" in data["visibility_window"]["upper"]

    async def test_clear_window_with_nulls(self, client: AsyncClient, db_session: AsyncSession) -> None:
        now = datetime.now(UTC)
        m = await _seed_module(
            db_session,
            visibility_window=Range(now, now + timedelta(days=7), lower_inc=True, upper_inc=False),
        )

        resp = await client.post(
            platform_path(f"/admin/modules/{m.id}/visibility-window"),
            json={"starts_at": None, "ends_at": None},
        )
        assert resp.status_code == 200
        assert resp.json()["visibility_window"] is None

    async def test_returns_404_for_unknown(self, client: AsyncClient) -> None:
        resp = await client.post(
            platform_path(f"/admin/modules/{uuid4()}/visibility-window"),
            json={"starts_at": None, "ends_at": None},
        )
        assert resp.status_code == 404


# ─── DELETE /admin/modules/{id} (retire) ───────────────────────────────────


class TestRetireEndpoint:
    async def test_delete_retires_module(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(db_session, title_localized={"bn": "bye"})

        resp = await client.delete(platform_path(f"/admin/modules/{m.id}"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["lifecycle_status"] == "retired"
        assert data["deprecated_at"] is not None

    async def test_delete_unknown_returns_404(self, client: AsyncClient) -> None:
        resp = await client.delete(platform_path(f"/admin/modules/{uuid4()}"))
        assert resp.status_code == 404


# ─── POST /admin/modules/search (semantic) ────────────────────────────────


class TestSemanticSearch:
    async def test_top_k_modules_by_cosine_distance(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        a = await _seed_module(db_session, title_localized={"bn": "A"}, embedding=_unit_basis_vector(0))
        await _seed_module(db_session, title_localized={"bn": "B"}, embedding=_unit_basis_vector(1))
        await _seed_module(db_session, title_localized={"bn": "C"}, embedding=_unit_basis_vector(2))

        resp = await client.post(
            platform_path("/admin/modules/search"),
            json={"query_vector": _unit_basis_vector(0), "limit": 3},
        )
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 3
        # Module A is rank 1 (cosine distance 0 to query).
        assert results[0]["id"] == str(a.id)

    async def test_skips_modules_without_embedding(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        with_emb = await _seed_module(
            db_session, title_localized={"bn": "indexed"}, embedding=_unit_basis_vector(0)
        )
        await _seed_module(db_session, title_localized={"bn": "not-indexed"}, embedding=None)

        resp = await client.post(
            platform_path("/admin/modules/search"),
            json={"query_vector": _unit_basis_vector(0), "limit": 5},
        )
        ids = {m["id"] for m in resp.json()}
        assert str(with_emb.id) in ids
        # Only one row returned — the unembedded module is skipped.
        assert len(ids) == 1

    async def test_query_string_embeds_via_ai_runtime(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the body sends `query` instead of `query_vector`, the endpoint
        embeds via ai-runtime and feeds the result into search_by_embedding.
        Mock the AIRuntimeClient.embed call to return a known vector."""

        target = await _seed_module(
            db_session, title_localized={"bn": "target"}, embedding=_unit_basis_vector(0)
        )
        await _seed_module(db_session, title_localized={"bn": "other"}, embedding=_unit_basis_vector(1))

        embed_mock = AsyncMock(return_value=[_unit_basis_vector(0)])
        monkeypatch.setattr(arc.AIRuntimeClient, "embed", embed_mock)

        resp = await client.post(
            platform_path("/admin/modules/search"), json={"query": "any text", "limit": 2}
        )
        assert resp.status_code == 200
        results = resp.json()
        assert results[0]["id"] == str(target.id)
        embed_mock.assert_awaited_once_with(["any text"])

    async def test_search_requires_query_or_vector(self, client: AsyncClient) -> None:
        resp = await client.post(platform_path("/admin/modules/search"), json={"limit": 3})
        assert resp.status_code == 400


# ─── Regenerate quiz / embedding ──────────────────────────────────────────


class TestRegeneratePostPublish:
    async def test_regenerate_quiz_enqueues_celery_task(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.generate_module_quiz_task, "delay", delay_mock)

        m = await _seed_module(db_session)
        resp = await client.post(platform_path(f"/admin/modules/{m.id}/regenerate-quiz"))
        assert resp.status_code == 200
        assert resp.json() == {
            "id": str(m.id),
            "enqueued": "platform.generate_module_quiz",
        }
        delay_mock.assert_called_once_with(str(m.id))

    async def test_regenerate_embedding_enqueues_celery_task(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        delay_mock = MagicMock()
        monkeypatch.setattr(celery_tasks.generate_module_embedding_task, "delay", delay_mock)

        m = await _seed_module(db_session)
        resp = await client.post(platform_path(f"/admin/modules/{m.id}/regenerate-embedding"))
        assert resp.status_code == 200
        assert resp.json() == {
            "id": str(m.id),
            "enqueued": "platform.generate_module_embedding",
        }
        delay_mock.assert_called_once_with(str(m.id))

    async def test_regenerate_quiz_404_when_module_missing(self, client: AsyncClient) -> None:
        resp = await client.post(platform_path(f"/admin/modules/{uuid4()}/regenerate-quiz"))
        assert resp.status_code == 404

    async def test_regenerate_embedding_404_when_module_missing(self, client: AsyncClient) -> None:
        resp = await client.post(platform_path(f"/admin/modules/{uuid4()}/regenerate-embedding"))
        assert resp.status_code == 404
