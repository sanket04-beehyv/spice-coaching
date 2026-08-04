"""Morning cards API endpoint tests.

Verifies:
- GET /morning/cards returns recently added published modules (fallback-only)
- GET /morning/cards?chw_id= uses gap-driven suggestions when available
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.api.morning import router as morning_router
from platform_service.config import get_settings
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.chw_behavioural_gap_state import CHWBehaviouralGapState
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.deps import get_db
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db, truncate_tables

pytestmark = [requires_db, pytest.mark.asyncio]


def _test_chw_id() -> int:
    return uuid4().int % (10**15) + 1


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(db_session, "chw_behavioural_gap_state, behavioural_gap, module, module_family")
    yield


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(morning_router)
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


async def _make_family(session: AsyncSession) -> ModuleFamily:
    fam = ModuleFamily(module_code=f"MF-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    return fam


async def _make_published_module(
    session: AsyncSession,
    *,
    family: ModuleFamily,
    tenant_id: UUID | None,
    primary_gap_id: UUID | None = None,
    created_at: datetime | None = None,
    set_family_pointer: bool = True,
) -> Module:
    now = datetime.now(UTC)
    mod = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": "t"},
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        tenant_id=tenant_id,
        primary_gap_id=primary_gap_id,
        module_json={"cards": [{"title": {"bn": "c"}}]},
        published_at=now,
        created_at=created_at or now,
    )
    session.add(mod)
    await session.flush()
    if primary_gap_id is not None:
        await ModuleGapRepository(session).add_primary_link(mod, behavioural_gap_id=primary_gap_id)
    if set_family_pointer:
        family.current_published_module_id = mod.id
        await session.flush()
    await session.commit()
    return mod


async def _make_gap(session: AsyncSession) -> BehaviouralGap:
    gap = BehaviouralGap(
        gap_code=f"gap_{uuid4().hex[:8]}",
        description="d",
        domain="rmnch",
        detection_rule_jsonb={},
    )
    session.add(gap)
    await session.flush()
    return gap


class TestMorningCardsEndpoint:
    async def test_without_chw_id_returns_recent_modules(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        tenant = UUID(int=0)
        base = datetime.now(UTC) - timedelta(days=10)
        modules: list[Module] = []
        for i in range(6):
            fam = await _make_family(db_session)
            m = await _make_published_module(
                db_session,
                family=fam,
                tenant_id=tenant,
                primary_gap_id=None,
                created_at=base + timedelta(hours=i),
            )
            modules.append(m)

        http_client = client
        resp = await http_client.get(platform_path("/morning/cards"))
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        items = data["items"]
        assert len(items) == 5
        assert all(x["source"] == "fallback" for x in items)
        expected_ids = {
            str(modules[5].id),
            str(modules[4].id),
            str(modules[3].id),
            str(modules[2].id),
            str(modules[1].id),
        }
        assert {x["module_id"] for x in items} == expected_ids

    async def test_with_chw_id_uses_gap_suggestions_when_available(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            get_settings(),
            "telemetry_behavioural_gap_state_enabled",
            True,
        )
        tenant = UUID(int=0)
        chw_id = _test_chw_id()
        gap = await _make_gap(db_session)
        db_session.add(
            CHWBehaviouralGapState(
                chw_id=chw_id,
                behavioural_gap_id=gap.id,
                tenant_id=tenant,
                status="active",
                severity_current="high",
                occurrence_count=3,
            )
        )
        await db_session.flush()

        fam = await _make_family(db_session)
        mod = await _make_published_module(
            db_session,
            family=fam,
            tenant_id=tenant,
            primary_gap_id=gap.id,
        )

        http_client = client
        resp = await http_client.get(platform_path("/morning/cards"), params={"chw_id": chw_id})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["module_id"] == str(mod.id)
        assert items[0]["module_family_id"] == str(fam.id)
        assert items[0]["source"] == "gap"
        assert items[0]["behavioural_gap_id"] == str(gap.id)

    async def test_with_chw_id_uses_quiz_suggestions_when_quiz_state_active(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        from platform_service.db.models.chw_quiz_question_state import CHWQuizQuestionState
        from platform_service.db.models.module_quiz_question import ModuleQuizQuestion

        tenant = UUID(int=0)
        chw_id = _test_chw_id()
        fam = await _make_family(db_session)
        mod = await _make_published_module(
            db_session,
            family=fam,
            tenant_id=tenant,
            primary_gap_id=None,
        )
        quiz = ModuleQuizQuestion(
            module_id=mod.id,
            question_order=1,
            question_family_id=uuid4(),
            question_version=1,
            question_localized={"bn": "q"},
            question_type="single_select",
            options_localized={"bn": ["a", "b"]},
            correct_indices=[0],
        )
        db_session.add(quiz)
        await db_session.flush()
        db_session.add(
            CHWQuizQuestionState(
                chw_id=chw_id,
                quiz_id=quiz.id,
                module_id=mod.id,
                tenant_id=tenant,
                failed_attempts_count=1,
                status="active",
                last_failed_attempt_at=datetime.now(UTC),
            )
        )
        await db_session.commit()

        resp = await client.get(platform_path("/morning/cards"), params={"chw_id": chw_id})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["module_id"] == str(mod.id)
        assert items[0]["source"] == "quiz"
        assert items[0]["quiz_id"] == str(quiz.id)
