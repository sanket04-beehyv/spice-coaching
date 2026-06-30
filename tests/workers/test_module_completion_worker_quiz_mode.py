"""module_completion_worker — quiz-id telemetry mode (default)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from platform_service.config import get_settings
from platform_service.db.models.chw_behavioural_gap_state import CHWBehaviouralGapState
from platform_service.db.models.chw_quiz_question_state import CHWQuizQuestionState
from platform_service.workers import module_completion_worker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db
from tests.workers.conftest import _add_quiz_questions, _make_gap, _make_module, _test_chw_id

pytestmark = [pytest.mark.asyncio, requires_db]


@pytest.fixture(autouse=True)
def _quiz_id_telemetry_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        get_settings(),
        "telemetry_behavioural_gap_state_enabled",
        False,
    )


@pytest.mark.asyncio
@requires_db
async def test_failing_quiz_updates_quiz_question_state_not_gap(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    questions = await _add_quiz_questions(db_session, module=module, count=1)
    quiz_id = questions[0].id
    chw = _test_chw_id()

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(quiz_id),
            "quiz_score_pct": 0.2,
        }
    )

    gap_row = await db_session.execute(
        select(CHWBehaviouralGapState).where(CHWBehaviouralGapState.chw_id == chw)
    )
    assert gap_row.first() is None

    quiz_row = await db_session.execute(
        select(CHWQuizQuestionState).where(
            CHWQuizQuestionState.chw_id == chw,
            CHWQuizQuestionState.quiz_id == quiz_id,
        )
    )
    state = quiz_row.scalar_one()
    assert state.failed_attempts_count == 1
    assert state.module_id == module.id
    assert state.status == "active"


@pytest.mark.asyncio
@requires_db
async def test_passing_quiz_resolves_quiz_question_state(
    patch_session_local, db_session: AsyncSession
) -> None:
    module = await _make_module(db_session)
    questions = await _add_quiz_questions(db_session, module=module, count=1)
    quiz_id = questions[0].id
    chw = _test_chw_id()
    db_session.add(
        CHWQuizQuestionState(
            chw_id=chw,
            quiz_id=quiz_id,
            module_id=module.id,
            failed_attempts_count=2,
            status="active",
        )
    )
    await db_session.flush()

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(quiz_id),
            "quiz_score_pct": 0.95,
        }
    )

    quiz_row = await db_session.execute(
        select(CHWQuizQuestionState).where(
            CHWQuizQuestionState.chw_id == chw,
            CHWQuizQuestionState.quiz_id == quiz_id,
        )
    )
    state = quiz_row.scalar_one()
    assert state.failed_attempts_count == 0
    assert state.status == "resolved"


@pytest.mark.asyncio
@requires_db
async def test_correct_outcome_resets_negative_failed_attempts_to_zero(
    patch_session_local, db_session: AsyncSession
) -> None:
    module = await _make_module(db_session)
    questions = await _add_quiz_questions(db_session, module=module, count=1)
    quiz_id = questions[0].id
    chw = _test_chw_id()
    db_session.add(
        CHWQuizQuestionState(
            chw_id=chw,
            quiz_id=quiz_id,
            module_id=module.id,
            failed_attempts_count=-1,
            status="active",
            escalated_to_supervisor=True,
        )
    )
    await db_session.flush()

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(quiz_id),
            "outcome": "correct",
            "quiz_score_pct": 0.1,
        }
    )

    quiz_row = await db_session.execute(
        select(CHWQuizQuestionState).where(
            CHWQuizQuestionState.chw_id == chw,
            CHWQuizQuestionState.quiz_id == quiz_id,
        )
    )
    state = quiz_row.scalar_one()
    assert state.failed_attempts_count == 0
    assert state.status == "resolved"
    assert state.escalated_to_supervisor is False
    assert state.last_failed_attempt_at is None


@pytest.mark.asyncio
@requires_db
async def test_correct_outcome_resets_positive_failed_attempts_to_zero(
    patch_session_local, db_session: AsyncSession
) -> None:
    module = await _make_module(db_session)
    questions = await _add_quiz_questions(db_session, module=module, count=1)
    quiz_id = questions[0].id
    chw = _test_chw_id()
    db_session.add(
        CHWQuizQuestionState(
            chw_id=chw,
            quiz_id=quiz_id,
            module_id=module.id,
            failed_attempts_count=3,
            status="active",
            escalated_to_supervisor=True,
            last_failed_attempt_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(quiz_id),
            "outcome": "correct",
            "quiz_score_pct": 0.1,
        }
    )

    quiz_row = await db_session.execute(
        select(CHWQuizQuestionState).where(
            CHWQuizQuestionState.chw_id == chw,
            CHWQuizQuestionState.quiz_id == quiz_id,
        )
    )
    state = quiz_row.scalar_one()
    assert state.failed_attempts_count == 0
    assert state.status == "resolved"
    assert state.escalated_to_supervisor is False
    assert state.last_failed_attempt_at is None
