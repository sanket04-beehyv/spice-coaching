"""Admin module deactivate/reactivate, lifecycle history, and analytics."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse
from platform_service.api.admin_module_analytics import router as admin_module_analytics_router
from platform_service.api.admin_modules import router as admin_modules_router
from platform_service.api.admin_trigger_bindings import router as admin_trigger_bindings_router
from platform_service.api.coaching_rag import router as coaching_rag_router
from platform_service.api.morning import router as morning_router
from platform_service.api.sync import router as sync_router
from platform_service.config import get_settings
from platform_service.db.models.chw_module_completion import CHWModuleCompletion
from platform_service.db.models.module import Module
from platform_service.db.models.trigger_definition import TriggerDefinition
from platform_service.deps import get_ai_client, get_db, get_object_storage_client
from platform_service.services.object_storage import PresignedObjectUrl
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.conftest import _seed_module, _unit_basis_vector
from tests.conftest import platform_path, requires_db
from tests.localized_helpers import loc, primary_from_response

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text(
            "TRUNCATE module_lifecycle_event, chw_module_completion, "
            "module_quiz_question, module_trigger_binding, trigger_definition, "
            "module, module_family, content_block, source_page, source_document "
            "RESTART IDENTITY CASCADE"
        )
    )
    await db_session.commit()


@pytest_asyncio.fixture
async def admin_app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(admin_modules_router)
    api_router.include_router(admin_module_analytics_router)
    api_router.include_router(admin_trigger_bindings_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    app_obj.dependency_overrides[get_object_storage_client] = lambda: MagicMock()
    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def admin_client(admin_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=admin_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def sync_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(sync_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    app_obj.dependency_overrides[get_object_storage_client] = lambda: MagicMock()
    transport = ASGITransport(app=app_obj)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def morning_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(morning_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app_obj)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def rag_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(coaching_rag_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    vec = _unit_basis_vector(0)
    ai_mock = MagicMock()
    ai_mock.embed = AsyncMock(return_value=[vec])
    ai_mock.generate = AsyncMock(
        return_value=InferenceResponse(
            request_id="test-req",
            generation_type=GenerationType.COACHING_RAG,
            provider="google",
            model="test",
            max_tokens=2048,
            temperature=0.2,
            latency_ms=1,
            raw_text='{"answer":"ok","cited_module_ids":[]}',
            parsed_json={"answer": "ok", "cited_module_ids": []},
        )
    )

    storage = MagicMock()
    storage.presigned_get_url = AsyncMock(
        return_value=PresignedObjectUrl(
            url="https://minio.test/obj",
            bucket_name="b",
            object_name="k",
            expires_seconds=3600,
        )
    )

    app_obj.dependency_overrides[get_db] = _override_get_db
    app_obj.dependency_overrides[get_ai_client] = lambda: ai_mock
    app_obj.dependency_overrides[get_object_storage_client] = lambda: storage
    transport = ASGITransport(app=app_obj)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app_obj.dependency_overrides.clear()


class TestModuleDeactivateReactivate:
    async def test_deactivate_draft_module_returns_409(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        mod = await _seed_module(db_session, title_localized=loc("draft only"), clinically_reviewed=False)
        mod.lifecycle_status = "draft"
        await db_session.commit()

        resp = await admin_client.post(platform_path(f"/admin/modules/{mod.id}/deactivate"))
        assert resp.status_code == 409

    async def test_invalid_lifecycle_status_returns_422(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.get(platform_path("/admin/modules?status=invalid"))
        assert resp.status_code == 422

    async def test_deactivate_and_reactivate_round_trip(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        mod = await _seed_module(db_session, title_localized=loc("ANC referral"))
        module_id = str(mod.id)
        actor_id = str(uuid4())

        deactivate = await admin_client.post(
            platform_path(f"/admin/modules/{module_id}/deactivate"),
            json={"actor_id": actor_id, "reason": "seasonal pause"},
        )
        assert deactivate.status_code == 200
        body = deactivate.json()
        assert body["lifecycle_status"] == "deactivated"
        assert body["last_deactivated_at"] is not None

        lifecycle = await admin_client.get(platform_path(f"/admin/modules/{module_id}/lifecycle"))
        assert lifecycle.status_code == 200
        events = lifecycle.json()
        assert any(e["event_type"] == "deactivated" for e in events)

        reactivate = await admin_client.post(
            platform_path(f"/admin/modules/{module_id}/reactivate"),
            json={"actor_id": actor_id, "reason": "resume program"},
        )
        assert reactivate.status_code == 200
        assert reactivate.json()["lifecycle_status"] == "published"
        assert reactivate.json()["last_reactivated_at"] is not None

        double = await admin_client.post(platform_path(f"/admin/modules/{module_id}/reactivate"))
        assert double.status_code == 409

    async def test_deactivate_unknown_module_returns_404(self, admin_client: AsyncClient) -> None:
        resp = await admin_client.post(platform_path(f"/admin/modules/{uuid4()}/deactivate"))
        assert resp.status_code == 404

    async def test_module_list_shows_lifecycle_status(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        mod = await _seed_module(db_session, title_localized=loc("status visible"))
        await admin_client.post(platform_path(f"/admin/modules/{mod.id}/deactivate"))

        deactivated_list = await admin_client.get(platform_path("/admin/modules?status=deactivated"))
        assert deactivated_list.status_code == 200
        row = next(
            m for m in deactivated_list.json()["modules"] if primary_from_response(m) == "status visible"
        )
        assert row["lifecycle_status"] == "deactivated"
        assert row["last_deactivated_at"] is not None

        default_list = await admin_client.get(platform_path("/admin/modules"))
        assert "status visible" not in {primary_from_response(m) for m in default_list.json()["modules"]}

        published_only = await admin_client.get(platform_path("/admin/modules?status=published"))
        assert all(m["lifecycle_status"] == "published" for m in published_only.json()["modules"])
        assert "status visible" not in {primary_from_response(m) for m in published_only.json()["modules"]}


class TestDeactivatedExcludedFromRuntime:
    async def test_sync_excludes_deactivated_module(
        self,
        admin_client: AsyncClient,
        sync_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        mod = await _seed_module(db_session, title_localized=loc("sync me not"))
        await admin_client.post(platform_path(f"/admin/modules/{mod.id}/deactivate"))

        since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        sync_resp = await sync_client.get(
            platform_path("/sync/modules"),
            params={"since": since},
        )
        assert sync_resp.status_code == 200
        module_ids = {m["id"] for m in sync_resp.json().get("modules", [])}
        assert str(mod.id) not in module_ids

    async def test_morning_cards_excludes_deactivated(
        self,
        admin_client: AsyncClient,
        morning_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        active = await _seed_module(db_session, title_localized=loc("active morning"))
        inactive = await _seed_module(db_session, title_localized=loc("inactive morning"))
        await admin_client.post(platform_path(f"/admin/modules/{inactive.id}/deactivate"))

        resp = await morning_client.get(platform_path("/morning/cards"))
        assert resp.status_code == 200
        family_ids = {item["module_family_id"] for item in resp.json()["items"]}
        assert str(active.module_family_id) in family_ids
        assert str(inactive.module_family_id) not in family_ids

    async def test_rag_excludes_deactivated_embedding_match(
        self,
        admin_client: AsyncClient,
        rag_client: AsyncClient,
        db_session: AsyncSession,
    ) -> None:
        vec = _unit_basis_vector(0)
        active = await _seed_module(db_session, title_localized=loc("active rag"), embedding=vec)
        inactive = await _seed_module(
            db_session,
            title_localized=loc("inactive rag"),
            embedding=_unit_basis_vector(1),
        )
        await admin_client.post(platform_path(f"/admin/modules/{inactive.id}/deactivate"))

        rag = await rag_client.post(
            platform_path("/coaching/rag-query"),
            json={"question": "test", "module_limit": 5},
        )
        assert rag.status_code == 200
        retrieved_ids = {m["module_id"] for m in rag.json()["retrieved_modules"]}
        assert str(active.id) in retrieved_ids
        assert str(inactive.id) not in retrieved_ids

    async def test_trigger_binding_rejected_for_deactivated_module(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        mod = await _seed_module(db_session, title_localized=loc("bound"))
        trigger = TriggerDefinition(
            trigger_kind="workflow",
            trigger_code=f"wf-{uuid4().hex[:6]}",
            predicate_jsonb={},
            status="active",
        )
        db_session.add(trigger)
        await db_session.commit()

        await admin_client.post(platform_path(f"/admin/modules/{mod.id}/deactivate"))
        bind = await admin_client.post(
            platform_path("/admin/trigger-bindings"),
            json={
                "trigger_definition_id": str(trigger.id),
                "module_family_id": str(mod.module_family_id),
            },
        )
        assert bind.status_code == 409


class TestModuleAnalytics:
    async def test_analytics_includes_deactivated_with_completion_history(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        mod = await _seed_module(db_session, title_localized=loc("analytics target"))
        family_id = mod.module_family_id
        now = datetime.now(UTC)
        db_session.add(
            CHWModuleCompletion(
                chw_id=42,
                module_family_id=family_id,
                latest_attempt_at=now - timedelta(days=2),
                latest_attempt_passed=True,
                completed_at=now - timedelta(days=2),
                attempts_since_last_pass=0,
            )
        )
        await db_session.commit()

        await admin_client.post(platform_path(f"/admin/modules/{mod.id}/deactivate"))

        from_dt = (now - timedelta(days=7)).isoformat()
        to_dt = (now + timedelta(days=1)).isoformat()
        resp = await admin_client.get(
            platform_path("/admin/analytics/modules"),
            params={"from": from_dt, "to": to_dt},
        )
        assert resp.status_code == 200
        rows = resp.json()
        match = next(r for r in rows if r["module_family_id"] == str(family_id))
        assert match["lifecycle_status"] == "deactivated"
        assert match["unique_chws_attempted"] >= 1
        assert match["unique_chws_completed"] >= 1
        assert match["family_created_at"] is not None

    async def test_analytics_lifecycle_status_filter(
        self, admin_client: AsyncClient, db_session: AsyncSession
    ) -> None:
        active = await _seed_module(db_session, title_localized=loc("active analytics"))
        inactive = await _seed_module(db_session, title_localized=loc("inactive analytics"))
        await admin_client.post(platform_path(f"/admin/modules/{inactive.id}/deactivate"))

        resp = await admin_client.get(platform_path("/admin/analytics/modules?lifecycle_status=deactivated"))
        assert resp.status_code == 200
        family_ids = {r["module_family_id"] for r in resp.json()}
        assert str(inactive.module_family_id) in family_ids
        assert str(active.module_family_id) not in family_ids


class TestModuleLifecycleRepository:
    async def test_reactivate_preserves_completion_rows(self, db_session: AsyncSession) -> None:
        from platform_service.db.repositories.module_lifecycle_repository import (
            ModuleLifecycleRepository,
        )

        mod = await _seed_module(db_session, title_localized=loc("persist"))
        family_id = mod.module_family_id
        db_session.add(
            CHWModuleCompletion(
                chw_id=7,
                module_family_id=family_id,
                latest_attempt_at=datetime.now(UTC),
                attempts_since_last_pass=1,
            )
        )
        await db_session.commit()

        repo = ModuleLifecycleRepository(db_session)
        await repo.deactivate(mod.id)
        await repo.reactivate(mod.id)
        await db_session.commit()

        result = await db_session.execute(
            select(CHWModuleCompletion).where(
                CHWModuleCompletion.chw_id == 7,
                CHWModuleCompletion.module_family_id == family_id,
            )
        )
        row = result.scalar_one_or_none()
        assert row is not None
        module = await db_session.get(Module, mod.id)
        assert module is not None
        assert module.lifecycle_status == "published"
