"""Shared fixtures for module_completion_worker tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from platform_service.config import get_settings
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.workers import module_completion_worker
from platform_service.workers.module_completion_worker import parse_uuid
from sqlalchemy.ext.asyncio import AsyncSession


def _test_chw_id() -> int:
    return uuid4().int % (10**15) + 1


@pytest.fixture(autouse=True)
def _gap_state_telemetry_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing worker tests target behavioural-gap telemetry mode."""
    monkeypatch.setenv("TELEMETRY_BEHAVIOURAL_GAP_STATE_ENABLED", "true")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _always_claim_module_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests often omit event_id; bypass idempotency claim only in that case."""
    original = module_completion_worker._try_claim_module_event

    async def _claim(session, payload) -> bool:
        event_id = payload.get("event_id")
        if not event_id or parse_uuid(event_id, field="event_id") is None:
            return True
        return await original(session, payload)

    monkeypatch.setattr(module_completion_worker, "_try_claim_module_event", _claim)


@pytest.fixture
def patch_session_local(db_session: AsyncSession):
    """Make module_completion_worker.SessionLocal yield our test session."""

    @asynccontextmanager
    async def _factory():
        # Disable internal commit so the test's rollback fixture cleans up.
        original_commit = db_session.commit

        async def _commit_as_flush() -> None:
            await db_session.flush()

        db_session.commit = _commit_as_flush  # type: ignore[method-assign]
        try:
            yield db_session
        finally:
            db_session.commit = original_commit  # type: ignore[method-assign]

    with patch.object(module_completion_worker, "SessionLocal", _factory):
        yield


async def _make_module(
    session: AsyncSession,
    *,
    primary_gap_id=None,
    version: int = 1,
) -> Module:
    family = ModuleFamily(module_code=f"WRK-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=version,
        lifecycle_status="published",
        module_type="refresher",
        title_localized={"bn": "মডিউল"},
        domain="hypertension",
        estimated_minutes=5,
        difficulty_level="basic",
        primary_gap_id=primary_gap_id,
    )
    session.add(module)
    await session.flush()
    family.current_published_module_id = module.id
    await session.flush()
    return module


async def _make_gap(session: AsyncSession) -> BehaviouralGap:
    gap = BehaviouralGap(
        gap_code=f"wrk_gap_{uuid4().hex[:8]}",
        description="x",
        domain="hypertension",
        detection_rule_jsonb={},
    )
    session.add(gap)
    await session.flush()
    return gap


async def _add_quiz_questions(
    session: AsyncSession,
    *,
    module: Module,
    count: int,
) -> list[ModuleQuizQuestion]:
    questions: list[ModuleQuizQuestion] = []
    for idx in range(count):
        q = ModuleQuizQuestion(
            module_id=module.id,
            question_order=idx + 1,
            question_family_id=uuid4(),
            question_version=1,
            question_localized={"bn": "q"},
            question_type="single_select",
            options_localized={"bn": ["a", "b"]},
            correct_indices=[0],
        )
        session.add(q)
        questions.append(q)
    await session.flush()
    return questions
