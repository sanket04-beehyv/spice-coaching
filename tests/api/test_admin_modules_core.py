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
from platform_service.deps import get_ai_client, get_object_storage_client
from sqlalchemy import select
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
    "synonyms": {"en": {}},
    "topic_tags": ["respiratory"],
    "clinical_conditions": [],
    "audience": "chw",
    "rationale": "",
}


class TestListModules:
    async def test_returns_summaries(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(db_session, title_localized={"bn": "Module A"})
        await _seed_module(db_session, title_localized={"bn": "Module B"})

        resp = await client.get(platform_path("/admin/modules"))
        assert resp.status_code == 200
        data = resp.json()
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
        assert resp.json()[0]["search_metadata"] is None

    async def test_search_metadata_round_trips_when_set(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        await _seed_module(db_session, search_metadata_jsonb=_SAMPLE_SEARCH_METADATA)
        resp = await client.get(platform_path("/admin/modules"))
        assert resp.json()[0]["search_metadata"] == _SAMPLE_SEARCH_METADATA

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
        assert resp.json()[0]["card_count"] == 3

    async def test_status_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(db_session, title_localized={"bn": "live"})
        await _seed_module(
            db_session, title_localized={"bn": "gone"}, lifecycle_status="retired", set_family_pointer=False
        )

        # Default excludes retired.
        default_resp = await client.get(platform_path("/admin/modules"))
        titles = {primary_from_response(m) for m in default_resp.json()}
        assert "live" in titles and "gone" not in titles

        # Explicit retired filter shows only retired.
        retired_resp = await client.get(platform_path("/admin/modules?status=retired"))
        titles = {primary_from_response(m) for m in retired_resp.json()}
        assert titles == {"gone"}

    async def test_clinically_reviewed_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(db_session, title_localized={"bn": "reviewed"}, clinically_reviewed=True)
        await _seed_module(db_session, title_localized={"bn": "pending"}, clinically_reviewed=False)

        resp = await client.get(platform_path("/admin/modules?clinically_reviewed=true"))
        titles = {primary_from_response(m) for m in resp.json()}
        assert titles == {"reviewed"}

        resp = await client.get(platform_path("/admin/modules?clinically_reviewed=false"))
        titles = {primary_from_response(m) for m in resp.json()}
        assert titles == {"pending"}

    async def test_full_text_query_filter(self, client: AsyncClient, db_session: AsyncSession) -> None:
        await _seed_module(db_session, title_localized={"bn": "Pregnancy referral"})
        await _seed_module(db_session, title_localized={"bn": "Diabetes screening"})

        resp = await client.get(platform_path("/admin/modules?q=referral"))
        titles = {primary_from_response(m) for m in resp.json()}
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
        assert {primary_from_response(m) for m in resp.json()} == {"windowed"}

        resp = await client.get(platform_path("/admin/modules?has_visibility_window=false"))
        assert {primary_from_response(m) for m in resp.json()} == {"no-window"}

    async def test_pagination_limit_offset(self, client: AsyncClient, db_session: AsyncSession) -> None:
        for i in range(5):
            await _seed_module(db_session, title_localized={"bn": f"m{i}"})

        resp = await client.get(platform_path("/admin/modules?limit=2&offset=0"))
        assert len(resp.json()) == 2
        resp = await client.get(platform_path("/admin/modules?limit=2&offset=2"))
        assert len(resp.json()) == 2
        resp = await client.get(platform_path("/admin/modules?limit=2&offset=4"))
        assert len(resp.json()) == 1

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
        rows = {primary_from_response(m): m for m in resp.json()}
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
        assert {primary_from_response(m) for m in resp.json()} == {"flagged"}

        resp = await client.get(platform_path("/admin/modules?has_quality_flags=false"))
        assert {primary_from_response(m) for m in resp.json()} == {"clean-null", "clean-empty"}


# ─── GET /admin/modules/{id} ───────────────────────────────────────────────


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
        assert "minio" in page_ref["presigned_url"]
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


# ─── PUT /admin/modules/{id} ───────────────────────────────────────────────


class TestEditModule:
    async def test_creates_new_version(self, client: AsyncClient, db_session: AsyncSession) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={"title": loc("v2 title")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["module_family_id"] == str(v1.module_family_id)
        assert body["version"] == 2
        assert body["supersedes_module_id"] == str(v1.id)
        # v2 has its own module id, distinct from v1.
        assert body["id"] != str(v1.id)

    async def test_adds_quiz_questions(self, client: AsyncClient, db_session: AsyncSession) -> None:
        v1 = await _seed_module(db_session, title_localized={"bn": "v1 title"})

        resp = await client.put(
            platform_path(f"/admin/modules/{v1.id}"),
            json={
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

    async def test_returns_404_for_unknown(self, client: AsyncClient) -> None:
        resp = await client.put(
            platform_path(f"/admin/modules/{uuid4()}"),
            json={"title": loc("noop")},
        )
        assert resp.status_code == 404

    async def test_returns_404_for_retired(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(db_session, lifecycle_status="retired", set_family_pointer=False)
        resp = await client.put(platform_path(f"/admin/modules/{m.id}"), json={"title": loc("won't take")})
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
            platform_path(f"/admin/modules/{v1.id}"), json={"module_json": module_json}
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
            platform_path(f"/admin/modules/{v1.id}"), json={"module_json": module_json}
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
            platform_path(f"/admin/modules/{v1.id}"), json={"module_json": module_json}
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
        resp = await client.put(platform_path(f"/admin/modules/{v1.id}"), json={"module_json": module_json})
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
                    "storage_path": "medtronics-storage/uploads/x.pdf",
                    "object_name": "uploads/x.pdf",
                    "content_type": "application/pdf",
                    "media_kind": "pdf",
                }
            ],
            "cards": [],
        }
        resp = await client.put(platform_path(f"/admin/modules/{v1.id}"), json={"module_json": module_json})
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
        app: FastAPI,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        """When the body sends `query` instead of `query_vector`, the endpoint
        embeds via ai-runtime and feeds the result into search_by_embedding."""

        target = await _seed_module(
            db_session, title_localized={"bn": "target"}, embedding=_unit_basis_vector(0)
        )
        await _seed_module(db_session, title_localized={"bn": "other"}, embedding=_unit_basis_vector(1))

        embed_mock = AsyncMock(return_value=[_unit_basis_vector(0)])
        stub = MagicMock()
        stub.embed = embed_mock
        app.dependency_overrides[get_ai_client] = lambda: stub

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
