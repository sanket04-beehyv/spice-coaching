"""module_completion_worker — delivery, noop, validation drops."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.chw_learning_point_event import CHWLearningPointEvent
from platform_service.db.models.chw_module_completion import CHWModuleCompletion
from platform_service.services.learning_points_thresholds import (
    LEARNING_POINTS_THRESHOLD_DEFAULTS,
    learning_points_delta_for_event,
)
from platform_service.workers import module_completion_worker
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db
from tests.workers.conftest import _make_gap, _make_module, _test_chw_id

pytestmark = [pytest.mark.asyncio, requires_db]


async def test_unhandled_event_type_no_op(patch_session_local, db_session: AsyncSession) -> None:
    await module_completion_worker.process_module_event_job(
        {"event_type": "card_shown", "chw_id": _test_chw_id(), "module_id": str(uuid4())}
    )
    # No state change to assert; just ensure it doesn't raise.


@pytest.mark.asyncio
@requires_db
async def test_module_delivered_awards_learning_points_no_completion_row(
    patch_session_local, db_session: AsyncSession
) -> None:
    chw = _test_chw_id()
    ev = str(uuid4())
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_delivered",
            "event_id": ev,
            "chw_id": chw,
            "module_id": str(uuid4()),
        }
    )
    r = await db_session.execute(select(CHWModuleCompletion))
    assert r.first() is None
    total = (
        await db_session.execute(
            select(func.coalesce(func.sum(CHWLearningPointEvent.points), 0)).where(
                CHWLearningPointEvent.chw_id == chw
            )
        )
    ).scalar_one()
    assert int(total) >= 1


@pytest.mark.asyncio
@requires_db
async def test_quiz_passing_score_without_outcome_does_not_award_learning_points(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    ev = uuid4()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(ev),
            "chw_id": chw,
            "module_id": str(module.id),
            "quiz_score_pct": 0.95,
        }
    )
    r = await db_session.execute(select(CHWLearningPointEvent).where(CHWLearningPointEvent.event_id == ev))
    assert r.scalar_one_or_none() is None


@pytest.mark.asyncio
@requires_db
async def test_quiz_outcome_correct_awards_learning_points_with_score_bonus(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    ev = uuid4()
    score = 0.2
    expected = learning_points_delta_for_event(
        "module_quiz_attempted",
        quiz_score_pct=score,
        thresholds=LEARNING_POINTS_THRESHOLD_DEFAULTS,
    )
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(ev),
            "chw_id": chw,
            "module_id": str(module.id),
            "quiz_score_pct": score,
            "outcome": "correct",
        }
    )
    row = (
        await db_session.execute(select(CHWLearningPointEvent).where(CHWLearningPointEvent.event_id == ev))
    ).scalar_one()
    assert row.points == expected
    assert row.chw_id == chw


@pytest.mark.asyncio
@requires_db
async def test_quiz_outcome_incorrect_high_score_does_not_award_learning_points(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    ev = uuid4()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(ev),
            "chw_id": chw,
            "module_id": str(module.id),
            "quiz_score_pct": 0.99,
            "outcome": "incorrect",
        }
    )
    r = await db_session.execute(select(CHWLearningPointEvent).where(CHWLearningPointEvent.event_id == ev))
    assert r.scalar_one_or_none() is None


@pytest.mark.asyncio
@requires_db
async def test_invalid_chw_id_drops_event(patch_session_local, db_session: AsyncSession) -> None:
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "chw_id": "not-an-int",
            "module_id": str(uuid4()),
            "quiz_score_pct": 0.8,
        }
    )
    r = await db_session.execute(select(CHWModuleCompletion))
    assert r.first() is None


@pytest.mark.asyncio
@requires_db
async def test_unknown_module_id_drops_event(patch_session_local, db_session: AsyncSession) -> None:
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "chw_id": _test_chw_id(),
            "module_id": str(uuid4()),  # not in DB
            "quiz_score_pct": 0.9,
        }
    )
    r = await db_session.execute(select(CHWModuleCompletion))
    assert r.first() is None
