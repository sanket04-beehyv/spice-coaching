"""Device sync route tests for chat FAQs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.api.sync import router as sync_router
from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.repositories.chat_faq_repository import ChatFaqRepository, ChatFaqRow
from platform_service.deps import get_db
from platform_service.services.chat_faq_aggregator import stable_faq_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text(
            "TRUNCATE chat_frequent_question, module_quiz_question, module, module_family "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(sync_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestSyncChatFaqs:
    async def test_requires_since(self, client: AsyncClient) -> None:
        tenant_id = uuid4()
        resp = await client.get(
            platform_path("/sync/chat-faqs"),
            params={"tenant_id": str(tenant_id)},
        )
        assert resp.status_code == 422

    async def test_returns_faqs_without_tenant_id(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        tenant_a = uuid4()
        tenant_b = uuid4()
        computed_at = datetime.now(UTC)
        repo = ChatFaqRepository(db_session)

        for tenant_id, question_en, question_bn in [
            (tenant_a, "How do I count respiratory rate?", "শ্বাসপ্রশ্বাসের হার কিভাবে গণনা করব?"),
            (tenant_b, "child cough", "কাশি"),
        ]:
            faq_id = stable_faq_id(tenant_id=tenant_id, normalized_question_en=question_en)
            await repo.replace_tenant_faqs(
                tenant_id,
                [
                    ChatFaqRow(
                        id=faq_id,
                        question_localized={"bn": question_bn, "en": question_en},
                        normalized_question=question_en,
                        occurrence_count=5,
                        rank=1,
                        last_seen_at=computed_at,
                    )
                ],
                computed_at=computed_at,
            )
        await db_session.commit()

        since = (computed_at - timedelta(seconds=1)).isoformat()
        resp = await client.get(platform_path("/sync/chat-faqs"), params={"since": since})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["faqs"]) == 2
        question_ens = {faq["question"]["en"] for faq in data["faqs"]}
        assert question_ens == {
            "How do I count respiratory rate?",
            "child cough",
        }

    async def test_returns_ranked_faqs_updated_since(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        tenant_id = uuid4()
        computed_at = datetime.now(UTC)
        question_en = "How do I count respiratory rate?"
        question_bn = "শ্বাসপ্রশ্বাসের হার কিভাবে গণনা করব?"
        faq_id = stable_faq_id(tenant_id=tenant_id, normalized_question_en=question_en)
        repo = ChatFaqRepository(db_session)
        await repo.replace_tenant_faqs(
            tenant_id,
            [
                ChatFaqRow(
                    id=faq_id,
                    question_localized={"bn": question_bn, "en": question_en},
                    normalized_question=question_en,
                    occurrence_count=7,
                    rank=1,
                    last_seen_at=computed_at,
                )
            ],
            computed_at=computed_at,
        )
        await db_session.commit()

        since = (computed_at - timedelta(seconds=1)).isoformat()
        resp = await client.get(
            platform_path("/sync/chat-faqs"),
            params={"since": since, "tenant_id": str(tenant_id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["faqs"]) == 1
        assert data["faqs"][0]["question"]["bn"] == question_bn
        assert data["faqs"][0]["question"]["en"] == question_en
        assert data["faqs"][0]["occurrence_count"] == 7
        assert data["faqs"][0]["rank"] == 1
        assert data["computed_at"] is not None

    async def test_empty_when_no_updates(self, client: AsyncClient, db_session: AsyncSession) -> None:
        tenant_id = uuid4()
        computed_at = datetime.now(UTC)
        question_en = "child cough"
        faq_id = stable_faq_id(tenant_id=tenant_id, normalized_question_en=question_en)
        repo = ChatFaqRepository(db_session)
        await repo.replace_tenant_faqs(
            tenant_id,
            [
                ChatFaqRow(
                    id=faq_id,
                    question_localized={"bn": "কাশি", "en": question_en},
                    normalized_question=question_en,
                    occurrence_count=3,
                    rank=1,
                    last_seen_at=computed_at,
                )
            ],
            computed_at=computed_at,
        )
        await db_session.commit()

        since = (computed_at + timedelta(seconds=1)).isoformat()
        resp = await client.get(
            platform_path("/sync/chat-faqs"),
            params={"since": since, "tenant_id": str(tenant_id)},
        )
        assert resp.status_code == 200
        assert resp.json()["faqs"] == []

    async def test_fallback_returns_quiz_questions_when_no_computed_faqs(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        tenant_id = uuid4()
        now = datetime.now(UTC)

        # Seed 6 published modules with descending published_at. One module is missing quiz questions
        # so the fallback should skip it and continue scanning older modules.
        module_ids_in_published_order: list[str] = []
        quiz_question_ids_by_module_id: dict[str, str] = {}
        quiz_question_bn_by_module_id: dict[str, str] = {}

        for i in range(6):
            fam = ModuleFamily(module_code=f"family-{uuid4().hex[:8]}")
            db_session.add(fam)
            await db_session.flush()

            published_at = now - timedelta(minutes=i)
            module = Module(
                module_family_id=fam.id,
                version=1,
                title_localized={"bn": f"Module {i}"},
                description_localized=None,
                domain="rmnch",
                sub_domain=None,
                module_type="refresher",
                tenant_id=tenant_id,
                estimated_minutes=10,
                difficulty_level="moderate",
                source_document_ids=None,
                thumbnail_storage_path=None,
                urgent_publish=False,
                module_json={"cards": [{"title": {"bn": "Card 1"}}]},
                embedding=None,
                visibility_window=None,
                pass_threshold_override=None,
                quality_flags_jsonb=None,
                search_metadata_jsonb=None,
                clinically_reviewed=False,
                clinically_reviewed_at=None,
                clinically_reviewed_by=None,
                lifecycle_status="published",
                published_at=published_at,
                deprecated_at=None,
                supersedes_module_id=None,
            )
            db_session.add(module)
            await db_session.flush()

            fam.current_published_module_id = module.id
            await db_session.flush()

            module_ids_in_published_order.append(str(module.id))

            # Skip quiz creation for the 2nd-newest module to ensure we skip-and-continue.
            if i == 1:
                continue

            q = ModuleQuizQuestion(
                module_id=module.id,
                question_order=1,
                question_family_id=uuid4(),
                question_version=1,
                question_localized={"bn": f"BN Q{i}", "en": f"EN Q{i}"},
                question_type="single_select",
                options_localized={"bn": ["a", "b", "c", "d"]},
                correct_indices=[0],
            )
            db_session.add(q)
            await db_session.flush()

            quiz_question_ids_by_module_id[str(module.id)] = str(q.id)
            quiz_question_bn_by_module_id[str(module.id)] = q.question_localized["bn"]

        await db_session.commit()

        since = (now - timedelta(days=365)).isoformat()
        resp = await client.get(
            platform_path("/sync/chat-faqs"),
            params={"since": since, "tenant_id": str(tenant_id)},
        )
        assert resp.status_code == 200
        data = resp.json()

        # No telemetry-derived FAQs exist, so we should get the fallback list.
        assert data["computed_at"] is None
        assert 1 <= len(data["faqs"]) <= 5

        # Build expected module order: newest to oldest, but skip the module without a quiz.
        expected_module_ids: list[str] = []
        for module_id in module_ids_in_published_order:
            if module_id in quiz_question_ids_by_module_id:
                expected_module_ids.append(module_id)
            if len(expected_module_ids) == 5:
                break

        assert len(data["faqs"]) == len(expected_module_ids)

        for idx, faq in enumerate(data["faqs"]):
            module_id = expected_module_ids[idx]
            assert faq["id"] == quiz_question_ids_by_module_id[module_id]
            assert faq["question"]["bn"] == quiz_question_bn_by_module_id[module_id]
            assert faq["question"]["en"].startswith("EN ")
            assert faq["occurrence_count"] == 0
            assert faq["rank"] == idx + 1
            assert faq["last_seen_at"] is None

    async def test_fallback_without_tenant_id(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)
        tenant_ids = [uuid4(), uuid4()]
        quiz_question_ids: list[str] = []

        for tenant_idx, tenant_id in enumerate(tenant_ids):
            fam = ModuleFamily(module_code=f"family-{uuid4().hex[:8]}")
            db_session.add(fam)
            await db_session.flush()

            module = Module(
                module_family_id=fam.id,
                version=1,
                title_localized={"bn": f"Module tenant {tenant_idx}"},
                description_localized=None,
                domain="rmnch",
                sub_domain=None,
                module_type="refresher",
                tenant_id=tenant_id,
                estimated_minutes=10,
                difficulty_level="moderate",
                source_document_ids=None,
                thumbnail_storage_path=None,
                urgent_publish=False,
                module_json={"cards": [{"title": {"bn": "Card 1"}}]},
                embedding=None,
                visibility_window=None,
                pass_threshold_override=None,
                quality_flags_jsonb=None,
                search_metadata_jsonb=None,
                clinically_reviewed=False,
                clinically_reviewed_at=None,
                clinically_reviewed_by=None,
                lifecycle_status="published",
                published_at=now - timedelta(minutes=tenant_idx),
                deprecated_at=None,
                supersedes_module_id=None,
            )
            db_session.add(module)
            await db_session.flush()

            fam.current_published_module_id = module.id
            await db_session.flush()

            q = ModuleQuizQuestion(
                module_id=module.id,
                question_order=1,
                question_family_id=uuid4(),
                question_version=1,
                question_localized={"bn": f"BN tenant {tenant_idx}", "en": f"EN tenant {tenant_idx}"},
                question_type="single_select",
                options_localized={"bn": ["a", "b", "c", "d"]},
                correct_indices=[0],
            )
            db_session.add(q)
            await db_session.flush()
            quiz_question_ids.append(str(q.id))

        await db_session.commit()

        since = (now - timedelta(days=365)).isoformat()
        resp = await client.get(platform_path("/sync/chat-faqs"), params={"since": since})
        assert resp.status_code == 200
        data = resp.json()

        assert data["computed_at"] is None
        assert len(data["faqs"]) == 2
        returned_ids = {faq["id"] for faq in data["faqs"]}
        assert returned_ids == set(quiz_question_ids)
