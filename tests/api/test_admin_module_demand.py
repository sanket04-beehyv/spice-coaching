"""API tests for admin module demand summary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, TokenUsage
from mc_foundation.problem import register_problem_handlers
from platform_service.api.admin_assignments import router as admin_assignments_router
from platform_service.api.admin_module_demand import router as admin_module_demand_router
from platform_service.config import get_settings
from platform_service.db.models.attribution_event import AttributionEvent
from platform_service.db.models.chw_training_request import CHWTrainingRequest
from platform_service.db.models.config_threshold import ConfigThreshold
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.deps import get_db
from platform_service.services.module_demand_service import ModuleDemandService
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]

# Known IDs from platform_service.services.user_service.get_all_users()
USER_A = 1313053891
USER_B = 1313053895
USER_C = 1313053892


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(
        db_session,
        "chw_training_request, chw_module_assignment, attribution_event, module_demand_summary, module, module_family",
    )
    await db_session.execute(text("DELETE FROM config_threshold WHERE key = 'module_demand_top_k'"))
    await db_session.commit()
    yield


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> FastAPI:
    app_obj = FastAPI()
    register_problem_handlers(
        app_obj,
        validation_error_type=RequestValidationError,
        http_exception_type=HTTPException,
    )

    @app_obj.middleware("http")
    async def mock_auth_middleware(request: Request, call_next):
        mock_user_id = request.headers.get("x-mock-user-id")
        if mock_user_id:

            class MockSpiceUser:
                id = int(mock_user_id)

            request.state.spice_user = MockSpiceUser()
        return await call_next(request)

    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(admin_module_demand_router)
    api_router.include_router(admin_assignments_router)
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


async def _seed_module(
    session: AsyncSession,
    title: str,
    *,
    lifecycle_status: str = "published",
    domain: str = "rmnch",
) -> Module:
    family = ModuleFamily(module_code=f"f-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": title, "en": title},
        domain=domain,
        module_type="refresher",
        lifecycle_status=lifecycle_status,
        module_json={"cards": [{"title": {"bn": "c"}}]},
        published_at=datetime.now(UTC) if lifecycle_status == "published" else None,
    )
    session.add(module)
    await session.flush()
    if lifecycle_status == "published":
        family.current_published_module_id = module.id
        await session.flush()
    await session.commit()
    return module


async def _seed_request(
    session: AsyncSession,
    *,
    chw_id: int,
    module_id=None,
    requested_module_name: str | None = None,
) -> CHWTrainingRequest:
    row = CHWTrainingRequest(
        chw_id=chw_id,
        module_id=module_id,
        requested_module_name=requested_module_name,
        reason=None,
        submitted_at=datetime.now(UTC),
        tenant_id=None,
    )
    session.add(row)
    await session.flush()
    await session.commit()
    return row


async def _seed_top_k(session: AsyncSession, value: int) -> None:
    session.add(
        ConfigThreshold(
            version=1,
            key="module_demand_top_k",
            value_json=value,
            title="Module Demand Top K",
            description="test",
        )
    )
    await session.commit()


def _failing_ai_client() -> MagicMock:
    client = MagicMock()
    client.generate = AsyncMock(side_effect=RuntimeError("ai down"))
    return client


def _ok_ai_client(text: str = "Demand is concentrated on ANC refresher modules.") -> MagicMock:
    client = MagicMock()
    client.generate = AsyncMock(
        return_value=InferenceResponse(
            request_id="test",
            generation_type=GenerationType.MODULE_DEMAND_SUMMARY,
            provider="google",
            model="test-model",
            max_tokens=8192,
            temperature=0.2,
            raw_text=text,
            parsed_json=None,
            latency_ms=1,
            token_usage=TokenUsage(),
            error=None,
        )
    )
    return client


def _mock_clickhouse(
    monkeypatch: pytest.MonkeyPatch,
    *,
    by_module: dict | None = None,
    requestors: list | None = None,
) -> MagicMock:
    analytics = MagicMock()
    analytics.distinct_chw_by_module_id = AsyncMock(return_value=by_module or {})
    analytics.requestors_for_module = AsyncMock(return_value=requestors or [])
    monkeypatch.setattr(
        "platform_service.services.module_demand_service.DashboardAnalyticsService",
        lambda _ch, _session: analytics,
    )
    monkeypatch.setattr(
        "platform_service.services.module_demand_service.get_clickhouse_client",
        lambda: MagicMock(),
    )
    return analytics


@pytest_asyncio.fixture(autouse=True)
def _default_clickhouse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid real ClickHouse in API tests; chatbot demand empty by default."""
    _mock_clickhouse(monkeypatch)


class TestAdminModuleDemand:
    async def test_summary_categorizes_published_draft_and_missing(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.get_ai_client",
            _failing_ai_client,
        )
        published = await _seed_module(db_session, "ANC Visit Checklist")
        draft = await _seed_module(db_session, "Postpartum Danger Signs", lifecycle_status="draft")

        await _seed_request(db_session, chw_id=USER_A, module_id=published.id)
        await _seed_request(db_session, chw_id=USER_B, module_id=published.id)
        await _seed_request(db_session, chw_id=USER_A, module_id=draft.id)
        await _seed_request(
            db_session,
            chw_id=USER_C,
            requested_module_name="Neonatal Resuscitation Advanced",
        )

        resp = await client.get(platform_path("/admin/module-demand/summary"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["top_k"] == 10
        assert "Top 10 module demand" in data["llm_summary"]
        assert len(data["available"]) == 2
        assert len(data["unavailable"]) == 1

        by_name = {item["display_name"]: item for item in data["available"] + data["unavailable"]}
        assert by_name["ANC Visit Checklist"]["action"] == "assign"
        assert by_name["ANC Visit Checklist"]["request_count"] == 2
        assert by_name["Postpartum Danger Signs"]["action"] == "open_draft"
        assert by_name["Postpartum Danger Signs"]["domain_filter"] == "rmnch"
        assert by_name["Neonatal Resuscitation Advanced"]["action"] == "create"

    async def test_free_text_title_match_folds_into_module(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.get_ai_client",
            _failing_ai_client,
        )
        published = await _seed_module(db_session, "Hypertension Screening")
        await _seed_request(
            db_session,
            chw_id=USER_A,
            requested_module_name="  hypertension screening ",
        )
        await _seed_request(db_session, chw_id=USER_B, module_id=published.id)

        resp = await client.get(platform_path("/admin/module-demand/summary"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["top_k"] == 10
        assert len(data["available"]) == 1
        assert data["unavailable"] == []
        item = data["available"][0]
        assert item["module_id"] == str(published.id)
        assert item["request_count"] == 2
        assert item["action"] == "assign"

    async def test_respects_configured_top_k(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.get_ai_client",
            _failing_ai_client,
        )
        await _seed_top_k(db_session, 1)
        m1 = await _seed_module(db_session, "Module Alpha")
        m2 = await _seed_module(db_session, "Module Beta")
        await _seed_request(db_session, chw_id=USER_A, module_id=m1.id)
        await _seed_request(db_session, chw_id=USER_B, module_id=m1.id)
        await _seed_request(db_session, chw_id=USER_C, module_id=m2.id)

        resp = await client.get(platform_path("/admin/module-demand/summary"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["top_k"] == 1
        assert len(data["available"]) + len(data["unavailable"]) == 1
        assert data["available"][0]["display_name"] == "Module Alpha"
        assert data["available"][0]["request_count"] == 2

    async def test_llm_success_uses_raw_text(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.get_ai_client",
            lambda: _ok_ai_client("Custom narrative about demand."),
        )
        published = await _seed_module(db_session, "ANC Visit Checklist")
        await _seed_request(db_session, chw_id=USER_A, module_id=published.id)

        resp = await client.get(platform_path("/admin/module-demand/summary"))
        assert resp.status_code == 200
        assert resp.json()["llm_summary"] == "Custom narrative about demand."

    async def test_llm_timeout_falls_back_to_soft_summary(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _slow_generate(_request):
            await asyncio.sleep(30)
            return _ok_ai_client("should not appear").generate.return_value

        client_mock = MagicMock()
        client_mock.generate = AsyncMock(side_effect=_slow_generate)
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.get_ai_client",
            lambda: client_mock,
        )
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.MODULE_DEMAND_LLM_TIMEOUT_SECONDS",
            0.05,
        )
        published = await _seed_module(db_session, "ANC Visit Checklist")
        await _seed_request(db_session, chw_id=USER_A, module_id=published.id)

        resp = await client.get(platform_path("/admin/module-demand/summary"))
        assert resp.status_code == 200
        assert "Top 10 module demand" in resp.json()["llm_summary"]

    async def test_requestors_marks_already_assigned(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.get_ai_client",
            _failing_ai_client,
        )
        published = await _seed_module(db_session, "ANC Visit Checklist")
        await _seed_request(db_session, chw_id=USER_A, module_id=published.id)
        await _seed_request(db_session, chw_id=USER_B, module_id=published.id)

        # Pre-assign USER_A via demand assign endpoint
        assign_resp = await client.post(
            platform_path(f"/admin/module-demand/modules/{published.id}/assign"),
            json={"user_ids": [USER_A]},
            headers={"x-mock-user-id": "1"},
        )
        assert assign_resp.status_code == 201
        assert assign_resp.json()["assigned_count"] == 1

        events = (
            (
                await db_session.execute(
                    select(AttributionEvent).where(AttributionEvent.event_type == "module_demand_assigned")
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].module_id == published.id
        assert events[0].payload_jsonb is not None
        assert events[0].payload_jsonb["source"] == "module_demand"

        resp = await client.get(platform_path(f"/admin/module-demand/modules/{published.id}/requestors"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["module_title"] == "ANC Visit Checklist"
        by_id = {r["chw_id"]: r for r in data["requestors"]}
        assert by_id[USER_A]["already_assigned"] is True
        assert by_id[USER_A]["source"] == "form"
        assert by_id[USER_B]["already_assigned"] is False

    async def test_requestors_404_for_unknown_module(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path(f"/admin/module-demand/modules/{uuid4()}/requestors"))
        assert resp.status_code == 404

    async def test_chatbot_requestors_merged_with_form(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.get_ai_client",
            _failing_ai_client,
        )
        published = await _seed_module(db_session, "ANC Visit Checklist")
        await _seed_request(db_session, chw_id=USER_A, module_id=published.id)
        _mock_clickhouse(
            monkeypatch,
            requestors=[(USER_C, datetime.now(UTC)), (USER_A, datetime.now(UTC))],
        )

        resp = await client.get(platform_path(f"/admin/module-demand/modules/{published.id}/requestors"))
        assert resp.status_code == 200
        by_id = {r["chw_id"]: r for r in resp.json()["requestors"]}
        assert by_id[USER_A]["source"] == "form"
        assert by_id[USER_A]["request_id"] is not None
        assert by_id[USER_C]["source"] == "chatbot"
        assert by_id[USER_C]["request_id"] is None

    async def test_summary_includes_chatbot_demand_counts(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.get_ai_client",
            _failing_ai_client,
        )
        published = await _seed_module(db_session, "Chat Hot Module")
        await _seed_request(db_session, chw_id=USER_A, module_id=published.id)
        _mock_clickhouse(
            monkeypatch,
            by_module={published.id: {USER_B, USER_C}},
        )

        resp = await client.get(platform_path("/admin/module-demand/summary"))
        assert resp.status_code == 200
        item = next(i for i in resp.json()["available"] if i["module_id"] == str(published.id))
        # USER_A (form) + USER_B + USER_C (chatbot)
        assert item["request_count"] == 3
        assert item["action"] == "assign"

    async def test_daily_refresh_snapshot_is_served_from_cache(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "platform_service.services.module_demand_service.get_ai_client",
            _failing_ai_client,
        )
        published = await _seed_module(db_session, "Cached Module")
        await _seed_request(db_session, chw_id=USER_A, module_id=published.id)

        # Precompute the snapshot (what the daily Celery job does).
        service = ModuleDemandService(db_session)
        await service.refresh_summary()

        # A later request is served from the snapshot even if new demand arrives.
        await _seed_request(db_session, chw_id=USER_B, module_id=published.id)
        resp = await client.get(platform_path("/admin/module-demand/summary"))
        assert resp.status_code == 200
        item = next(i for i in resp.json()["available"] if i["module_id"] == str(published.id))
        # Snapshot captured only USER_A; USER_B lands in the next refresh.
        assert item["request_count"] == 1
