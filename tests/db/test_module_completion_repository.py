"""W-10 — module_completion_repository integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.module_completion_repository import (
    ModuleCompletionRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db


def _test_chw_id() -> int:
    return uuid4().int % (10**15) + 1


async def _make_family(session: AsyncSession) -> ModuleFamily:
    family = ModuleFamily(module_code=f"COMP-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    return family


@pytest.mark.asyncio
@requires_db
async def test_first_quiz_pass_creates_row_and_schedules_reinforcement(
    db_session: AsyncSession,
) -> None:
    family = await _make_family(db_session)
    chw = _test_chw_id()
    repo = ModuleCompletionRepository(db_session)
    module_id = uuid4()
    comp = await repo.record_quiz_attempt(
        chw_id=chw,
        module_family_id=family.id,
        attempted_module_id=module_id,
        score_pct=0.85,
        passed=True,
        reinforcement_days=90,
    )
    assert comp.latest_attempt_passed is True
    assert comp.latest_completed_module_id == module_id
    assert comp.latest_attempt_module_id == module_id
    assert comp.latest_quiz_score == 0.85
    assert comp.attempts_since_last_pass == 0
    assert comp.completed_at is not None
    # +90 days reinforcement window
    expected_due = datetime.now(UTC) + timedelta(days=90)
    delta = abs((comp.reinforcement_due_at - expected_due).total_seconds())
    assert delta < 5  # within 5 seconds of "now + 90d"


@pytest.mark.asyncio
@requires_db
async def test_first_quiz_fail_creates_row_with_attempt_counter(
    db_session: AsyncSession,
) -> None:
    family = await _make_family(db_session)
    chw = _test_chw_id()
    repo = ModuleCompletionRepository(db_session)
    comp = await repo.record_quiz_attempt(
        chw_id=chw,
        module_family_id=family.id,
        attempted_module_id=uuid4(),
        score_pct=0.40,
        passed=False,
    )
    assert comp.latest_attempt_passed is False
    assert comp.latest_completed_module_id is None
    assert comp.attempts_since_last_pass == 1
    assert comp.completed_at is None
    assert comp.reinforcement_due_at is None


@pytest.mark.asyncio
@requires_db
async def test_repeated_failures_increment_counter(db_session: AsyncSession) -> None:
    family = await _make_family(db_session)
    chw = _test_chw_id()
    repo = ModuleCompletionRepository(db_session)
    for _ in range(3):
        comp = await repo.record_quiz_attempt(
            chw_id=chw,
            module_family_id=family.id,
            attempted_module_id=uuid4(),
            score_pct=0.20,
            passed=False,
        )
    assert comp.attempts_since_last_pass == 3


@pytest.mark.asyncio
@requires_db
async def test_pass_after_failures_resets_counter_and_marks_completed(
    db_session: AsyncSession,
) -> None:
    family = await _make_family(db_session)
    chw = _test_chw_id()
    repo = ModuleCompletionRepository(db_session)
    for _ in range(2):
        await repo.record_quiz_attempt(
            chw_id=chw,
            module_family_id=family.id,
            attempted_module_id=uuid4(),
            score_pct=0.40,
            passed=False,
        )
    pass_module = uuid4()
    comp = await repo.record_quiz_attempt(
        chw_id=chw,
        module_family_id=family.id,
        attempted_module_id=pass_module,
        score_pct=0.80,
        passed=True,
    )
    assert comp.attempts_since_last_pass == 0
    assert comp.latest_completed_module_id == pass_module
    assert comp.completed_at is not None


@pytest.mark.asyncio
@requires_db
async def test_mark_completed_no_op_when_no_prior_attempt(
    db_session: AsyncSession,
) -> None:
    """If MODULE_COMPLETED arrives without a prior MODULE_QUIZ_ATTEMPTED
    creating the row, we don't fabricate one (no score data to record)."""
    family = await _make_family(db_session)
    repo = ModuleCompletionRepository(db_session)
    out = await repo.mark_completed(
        chw_id=_test_chw_id(),
        module_family_id=family.id,
        completed_module_id=uuid4(),
    )
    assert out is None


@pytest.mark.asyncio
@requires_db
async def test_mark_completed_after_existing_attempt_stamps_timestamp(
    db_session: AsyncSession,
) -> None:
    family = await _make_family(db_session)
    chw = _test_chw_id()
    repo = ModuleCompletionRepository(db_session)
    await repo.record_quiz_attempt(
        chw_id=chw,
        module_family_id=family.id,
        attempted_module_id=uuid4(),
        score_pct=0.80,
        passed=True,
    )
    later_module = uuid4()
    later_at = datetime.now(UTC) + timedelta(minutes=5)
    out = await repo.mark_completed(
        chw_id=chw,
        module_family_id=family.id,
        completed_module_id=later_module,
        completed_at=later_at,
    )
    assert out is not None
    assert out.latest_completed_module_id == later_module
    assert out.completed_at == later_at


@pytest.mark.asyncio
@requires_db
async def test_clear_completion_stamp_clears_stamped_row(db_session: AsyncSession) -> None:
    family = await _make_family(db_session)
    chw = _test_chw_id()
    repo = ModuleCompletionRepository(db_session)
    completed_module = uuid4()
    await repo.record_quiz_attempt(
        chw_id=chw,
        module_family_id=family.id,
        attempted_module_id=completed_module,
        score_pct=0.80,
        passed=True,
    )
    out = await repo.clear_completion_stamp(chw_id=chw, module_family_id=family.id)
    assert out is not None
    assert out.completed_at is None
    assert out.latest_completed_module_id is None
    assert out.latest_attempt_passed is True


@pytest.mark.asyncio
@requires_db
async def test_clear_completion_stamp_no_op_when_not_stamped(db_session: AsyncSession) -> None:
    family = await _make_family(db_session)
    chw = _test_chw_id()
    repo = ModuleCompletionRepository(db_session)
    await repo.record_quiz_attempt(
        chw_id=chw,
        module_family_id=family.id,
        attempted_module_id=uuid4(),
        score_pct=0.40,
        passed=False,
    )
    out = await repo.clear_completion_stamp(chw_id=chw, module_family_id=family.id)
    assert out is not None
    assert out.completed_at is None


@pytest.mark.asyncio
@requires_db
async def test_list_due_for_reinforcement(db_session: AsyncSession) -> None:
    family_a = await _make_family(db_session)
    family_b = await _make_family(db_session)
    chw = _test_chw_id()
    repo = ModuleCompletionRepository(db_session)
    # Module A: passed 100 days ago — due
    comp_a = await repo.record_quiz_attempt(
        chw_id=chw,
        module_family_id=family_a.id,
        attempted_module_id=uuid4(),
        score_pct=0.9,
        passed=True,
    )
    comp_a.reinforcement_due_at = datetime.now(UTC) - timedelta(days=10)
    # Module B: passed 1 day ago — not due
    await repo.record_quiz_attempt(
        chw_id=chw,
        module_family_id=family_b.id,
        attempted_module_id=uuid4(),
        score_pct=0.9,
        passed=True,
    )
    await db_session.flush()
    due = await repo.list_due_for_reinforcement(chw_id=chw)
    family_ids = [d.module_family_id for d in due]
    assert family_a.id in family_ids
    assert family_b.id not in family_ids
