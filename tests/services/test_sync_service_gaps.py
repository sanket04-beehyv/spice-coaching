"""SyncService.get_gaps_bundle — partial module quiz progress."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from platform_service.db.models.chw_module_completion import CHWModuleCompletion
from platform_service.db.models.chw_module_quiz_progress import CHWModuleQuizProgress
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.repositories.module_completion_repository import (
    ModuleCompletionRepository,
)
from platform_service.services.sync_service import SyncService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db


def _test_chw_id() -> int:
    return uuid4().int % (10**15) + 1


async def _make_module(session: AsyncSession) -> Module:
    family = ModuleFamily(module_code=f"SYNC-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        lifecycle_status="published",
        module_type="refresher",
        title_localized={"bn": "মডিউল"},
        domain="hypertension",
        estimated_minutes=5,
        difficulty_level="basic",
    )
    session.add(module)
    await session.flush()
    family.current_published_module_id = module.id
    await session.flush()
    return module


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


async def _add_progress(
    session: AsyncSession,
    *,
    chw_id: int,
    module: Module,
    quiz_id,
    first_correct_at: datetime | None = None,
) -> None:
    row = CHWModuleQuizProgress(
        chw_id=chw_id,
        module_id=module.id,
        quiz_id=quiz_id,
    )
    if first_correct_at is not None:
        row.first_correct_at = first_correct_at
    session.add(row)
    await session.flush()


@pytest.mark.asyncio
@requires_db
async def test_partial_completion_two_of_three_questions(db_session: AsyncSession) -> None:
    chw_id = _test_chw_id()
    module = await _make_module(db_session)
    q1, q2, q3 = await _add_quiz_questions(db_session, module=module, count=3)
    await _add_progress(db_session, chw_id=chw_id, module=module, quiz_id=q1.id)
    await _add_progress(db_session, chw_id=chw_id, module=module, quiz_id=q2.id)

    bundle = await SyncService(db_session).get_gaps_bundle(since=None, chw_id=chw_id)

    assert len(bundle.chw_module_partial_completions) == 1
    partial = bundle.chw_module_partial_completions[0]
    assert partial.chw_id == chw_id
    assert partial.module_id == module.id
    assert partial.module_family_id == module.module_family_id
    assert partial.incomplete_quiz_ids == [q3.id]


@pytest.mark.asyncio
@requires_db
async def test_partial_completion_empty_when_all_questions_answered(
    db_session: AsyncSession,
) -> None:
    chw_id = _test_chw_id()
    module = await _make_module(db_session)
    q1, q2 = await _add_quiz_questions(db_session, module=module, count=2)
    await _add_progress(db_session, chw_id=chw_id, module=module, quiz_id=q1.id)
    await _add_progress(db_session, chw_id=chw_id, module=module, quiz_id=q2.id)

    db_session.add(
        CHWModuleCompletion(
            chw_id=chw_id,
            module_family_id=module.module_family_id,
            latest_attempt_module_id=module.id,
            latest_attempt_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    repo = ModuleCompletionRepository(db_session)
    await repo.mark_completed(
        chw_id=chw_id,
        module_family_id=module.module_family_id,
        completed_module_id=module.id,
    )
    await db_session.flush()

    bundle = await SyncService(db_session).get_gaps_bundle(since=None, chw_id=chw_id)

    assert bundle.chw_module_partial_completions == []
    assert len(bundle.chw_module_completions) == 1


@pytest.mark.asyncio
@requires_db
async def test_partial_completion_empty_without_progress_rows(db_session: AsyncSession) -> None:
    chw_id = _test_chw_id()
    await _make_module(db_session)

    bundle = await SyncService(db_session).get_gaps_bundle(since=None, chw_id=chw_id)

    assert bundle.chw_module_partial_completions == []


@pytest.mark.asyncio
@requires_db
async def test_partial_completion_omitted_when_chw_id_not_provided(db_session: AsyncSession) -> None:
    bundle = await SyncService(db_session).get_gaps_bundle(since=None, chw_id=None)

    assert bundle.chw_module_partial_completions == []


@pytest.mark.asyncio
@requires_db
async def test_partial_completion_since_after_last_progress(db_session: AsyncSession) -> None:
    chw_id = _test_chw_id()
    module = await _make_module(db_session)
    q1, q2 = await _add_quiz_questions(db_session, module=module, count=2)
    old = datetime.now(UTC) - timedelta(days=2)
    await _add_progress(
        db_session,
        chw_id=chw_id,
        module=module,
        quiz_id=q1.id,
        first_correct_at=old,
    )
    await _add_progress(
        db_session,
        chw_id=chw_id,
        module=module,
        quiz_id=q2.id,
        first_correct_at=old,
    )

    since = datetime.now(UTC) - timedelta(hours=1)
    bundle = await SyncService(db_session).get_gaps_bundle(since=since, chw_id=chw_id)

    assert bundle.chw_module_partial_completions == []


@pytest.mark.asyncio
@requires_db
async def test_partial_completion_since_before_last_progress(db_session: AsyncSession) -> None:
    chw_id = _test_chw_id()
    module = await _make_module(db_session)
    q1, q2, q3 = await _add_quiz_questions(db_session, module=module, count=3)
    old = datetime.now(UTC) - timedelta(days=2)
    await _add_progress(
        db_session,
        chw_id=chw_id,
        module=module,
        quiz_id=q1.id,
        first_correct_at=old,
    )
    await _add_progress(db_session, chw_id=chw_id, module=module, quiz_id=q2.id)

    since = datetime.now(UTC) - timedelta(hours=1)
    bundle = await SyncService(db_session).get_gaps_bundle(since=since, chw_id=chw_id)

    assert len(bundle.chw_module_partial_completions) == 1
    assert bundle.chw_module_partial_completions[0].incomplete_quiz_ids == [q3.id]


@pytest.mark.asyncio
@requires_db
async def test_quiz_question_states_in_bundle_when_quiz_telemetry_mode(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from platform_service.config import get_settings
    from platform_service.db.models.chw_quiz_question_state import CHWQuizQuestionState

    monkeypatch.setattr(
        get_settings(),
        "telemetry_behavioural_gap_state_enabled",
        False,
    )
    chw_id = _test_chw_id()
    module = await _make_module(db_session)
    (q1,) = await _add_quiz_questions(db_session, module=module, count=1)
    db_session.add(
        CHWQuizQuestionState(
            chw_id=chw_id,
            quiz_id=q1.id,
            module_id=module.id,
            failed_attempts_count=1,
            status="active",
        )
    )
    await db_session.flush()

    bundle = await SyncService(db_session).get_gaps_bundle(since=None, chw_id=chw_id)

    assert bundle.chw_behavioural_gap_states == []
    assert len(bundle.chw_quiz_question_states) == 1
    assert bundle.chw_quiz_question_states[0].quiz_id == q1.id
    assert bundle.chw_quiz_question_states[0].failed_attempts_count == 1
