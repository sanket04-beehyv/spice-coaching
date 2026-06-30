"""module_completion_worker — module_quiz_attempted and version attribution."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.chw_behavioural_gap_state import CHWBehaviouralGapState
from platform_service.db.models.chw_module_completion import CHWModuleCompletion
from platform_service.db.models.chw_module_quiz_progress import CHWModuleQuizProgress
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.workers import module_completion_worker
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db
from tests.workers.conftest import _add_quiz_questions, _make_gap, _make_module, _test_chw_id

pytestmark = [pytest.mark.asyncio, requires_db]

# ── quiz attempt happy paths ────────────────────────────────────────────


@pytest.mark.asyncio
@requires_db
async def test_passing_quiz_resets_gap_failures(patch_session_local, db_session: AsyncSession) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    # seed: prior failed attempts so we can verify reset
    db_session.add(
        CHWBehaviouralGapState(
            chw_id=chw,
            behavioural_gap_id=gap.id,
            failed_attempts_count=2,
            escalated_to_supervisor=True,
        )
    )
    await db_session.flush()

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_score_pct": 0.85,
        }
    )

    r = await db_session.execute(select(CHWModuleCompletion))
    assert r.first() is None

    # Gap state reset: failed_attempts_count cleared, no longer escalated
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.failed_attempts_count == 0
    assert state.escalated_to_supervisor is False
    assert state.last_reinforced_at is not None
    assert state.first_observed_at is not None
    assert state.last_observed_at is not None


@pytest.mark.asyncio
@requires_db
async def test_failing_quiz_increments_gap_failed_attempts(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_score_pct": 0.40,
        }
    )
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.failed_attempts_count == 1
    assert state.occurrence_count == 1
    assert state.first_observed_at is not None
    assert state.last_observed_at is not None


@pytest.mark.asyncio
@requires_db
async def test_quiz_outcome_incorrect_increments_failures_even_when_score_passes(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_score_pct": 0.95,
            "outcome": "incorrect",
        }
    )
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.failed_attempts_count == 1


@pytest.mark.asyncio
@requires_db
async def test_quiz_outcome_correct_decrements_failed_attempts(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    db_session.add(
        CHWBehaviouralGapState(
            chw_id=chw,
            behavioural_gap_id=gap.id,
            failed_attempts_count=3,
            escalated_to_supervisor=True,
            status="active",
        )
    )
    await db_session.flush()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_score_pct": 0.20,
            "outcome": "correct",
        }
    )
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.failed_attempts_count == 2
    assert state.escalated_to_supervisor is False
    assert state.status == "active"
    assert state.last_observed_at is not None


@pytest.mark.asyncio
@requires_db
async def test_second_quiz_attempt_increments_gap_occurrence_count(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    job = {
        "event_type": "module_quiz_attempted",
        "chw_id": str(chw),
        "module_id": str(module.id),
        "quiz_score_pct": 0.40,
    }
    await module_completion_worker.process_module_event_job({**job, "event_id": "evt-quiz-a"})
    await module_completion_worker.process_module_event_job({**job, "event_id": "evt-quiz-b"})
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.occurrence_count == 2
    assert state.first_observed_at is not None
    assert state.last_observed_at is not None


@pytest.mark.asyncio
@requires_db
async def test_quiz_outcome_correct_hitting_zero_sets_resolved(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    db_session.add(
        CHWBehaviouralGapState(
            chw_id=chw,
            behavioural_gap_id=gap.id,
            failed_attempts_count=1,
            status="active",
        )
    )
    await db_session.flush()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_score_pct": 0.0,
            "outcome": "correct",
        }
    )
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.failed_attempts_count == 0
    assert state.status == "resolved"


@pytest.mark.asyncio
@requires_db
async def test_three_fails_in_window_escalates_via_gap_state_service(
    patch_session_local, db_session: AsyncSession
) -> None:
    """Verifies we reuse the W-8 escalation rule rather than reimplementing it."""
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    for _ in range(3):
        await module_completion_worker.process_module_event_job(
            {
                "event_type": "module_quiz_attempted",
                "chw_id": str(chw),
                "module_id": str(module.id),
                "quiz_score_pct": 0.30,
            }
        )
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.failed_attempts_count == 3
    assert state.escalated_to_supervisor is True


@pytest.mark.asyncio
@requires_db
async def test_quiz_event_for_module_without_primary_gap_skips_gap_update(
    patch_session_local, db_session: AsyncSession
) -> None:
    """Modules without a primary_gap_id (broad refreshers) skip quiz-driven
    gap updates."""
    module = await _make_module(db_session, primary_gap_id=None)
    q1 = (await _add_quiz_questions(db_session, module=module, count=1))[0]
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_score_pct": 0.90,
            "quiz_id": str(q1.id),
            "outcome": "incorrect",
        }
    )
    r = await db_session.execute(select(CHWBehaviouralGapState))
    assert r.first() is None
    r = await db_session.execute(
        select(func.count())
        .select_from(CHWModuleQuizProgress)
        .where(CHWModuleQuizProgress.chw_id == chw, CHWModuleQuizProgress.module_id == module.id)
    )
    assert int(r.scalar_one()) == 1
    r = await db_session.execute(select(CHWModuleCompletion).where(CHWModuleCompletion.chw_id == chw))
    comp = r.scalar_one()
    assert comp.latest_completed_module_id == module.id


@pytest.mark.asyncio
@requires_db
async def test_quiz_event_with_missing_score_treated_as_zero_fail(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    module = await _make_module(db_session, primary_gap_id=gap.id)
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "chw_id": str(chw),
            "module_id": str(module.id),
            # quiz_score_pct intentionally omitted
        }
    )
    r = await db_session.execute(select(CHWModuleCompletion))
    assert r.first() is None
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    assert r.scalar_one().failed_attempts_count == 1


@pytest.mark.asyncio
@requires_db
async def test_quiz_correct_records_progress_and_marks_module_completed_when_coverage_hits_100pct(
    patch_session_local, db_session: AsyncSession
) -> None:
    module = await _make_module(db_session, primary_gap_id=None)
    q1, q2 = await _add_quiz_questions(db_session, module=module, count=2)
    chw = _test_chw_id()

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(q1.id),
            "quiz_score_pct": 0.10,
            "outcome": "correct",
        }
    )
    r = await db_session.execute(select(CHWModuleCompletion).where(CHWModuleCompletion.chw_id == chw))
    assert r.scalar_one_or_none() is None
    r = await db_session.execute(
        select(func.count())
        .select_from(CHWModuleQuizProgress)
        .where(CHWModuleQuizProgress.chw_id == chw, CHWModuleQuizProgress.module_id == module.id)
    )
    assert int(r.scalar_one()) == 1

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(q2.id),
            "quiz_score_pct": 0.10,
            "outcome": "correct",
        }
    )
    r = await db_session.execute(select(CHWModuleCompletion).where(CHWModuleCompletion.chw_id == chw))
    comp = r.scalar_one()
    assert comp.completed_at is not None
    assert comp.latest_completed_module_id == module.id


@pytest.mark.asyncio
@requires_db
async def test_quiz_incorrect_records_progress_and_marks_module_completed_when_coverage_hits_100pct(
    patch_session_local, db_session: AsyncSession
) -> None:
    module = await _make_module(db_session, primary_gap_id=None)
    q1, q2 = await _add_quiz_questions(db_session, module=module, count=2)
    chw = _test_chw_id()

    for quiz in (q1, q2):
        await module_completion_worker.process_module_event_job(
            {
                "event_type": "module_quiz_attempted",
                "event_id": str(uuid4()),
                "chw_id": str(chw),
                "module_id": str(module.id),
                "quiz_id": str(quiz.id),
                "quiz_score_pct": 0.10,
                "outcome": "incorrect",
            }
        )

    r = await db_session.execute(
        select(func.count())
        .select_from(CHWModuleQuizProgress)
        .where(CHWModuleQuizProgress.chw_id == chw, CHWModuleQuizProgress.module_id == module.id)
    )
    assert int(r.scalar_one()) == 2

    r = await db_session.execute(select(CHWModuleCompletion).where(CHWModuleCompletion.chw_id == chw))
    comp = r.scalar_one()
    assert comp.completed_at is not None
    assert comp.latest_completed_module_id == module.id


@pytest.mark.asyncio
@requires_db
async def test_quiz_correct_duplicate_question_is_idempotent(
    patch_session_local, db_session: AsyncSession
) -> None:
    module = await _make_module(db_session, primary_gap_id=None)
    q1 = (await _add_quiz_questions(db_session, module=module, count=1))[0]
    chw = _test_chw_id()

    payload = {
        "event_type": "module_quiz_attempted",
        "event_id": str(uuid4()),
        "chw_id": str(chw),
        "module_id": str(module.id),
        "quiz_id": str(q1.id),
        "quiz_score_pct": 0.10,
        "outcome": "correct",
    }
    await module_completion_worker.process_module_event_job(payload)
    await module_completion_worker.process_module_event_job({**payload, "event_id": str(uuid4())})

    r = await db_session.execute(
        select(func.count())
        .select_from(CHWModuleQuizProgress)
        .where(CHWModuleQuizProgress.chw_id == chw, CHWModuleQuizProgress.module_id == module.id)
    )
    assert int(r.scalar_one()) == 1


@pytest.mark.asyncio
@requires_db
async def test_quiz_incorrect_records_progress(patch_session_local, db_session: AsyncSession) -> None:
    module = await _make_module(db_session, primary_gap_id=None)
    q1 = (await _add_quiz_questions(db_session, module=module, count=1))[0]
    chw = _test_chw_id()

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(q1.id),
            "quiz_score_pct": 0.99,
            "outcome": "incorrect",
        }
    )
    r = await db_session.execute(
        select(func.count())
        .select_from(CHWModuleQuizProgress)
        .where(CHWModuleQuizProgress.chw_id == chw, CHWModuleQuizProgress.module_id == module.id)
    )
    assert int(r.scalar_one()) == 1


@pytest.mark.asyncio
@requires_db
async def test_quiz_incorrect_after_correct_keeps_progress(
    patch_session_local, db_session: AsyncSession
) -> None:
    module = await _make_module(db_session, primary_gap_id=None)
    q1 = (await _add_quiz_questions(db_session, module=module, count=1))[0]
    chw = _test_chw_id()

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(q1.id),
            "quiz_score_pct": 0.10,
            "outcome": "correct",
        }
    )
    r = await db_session.execute(
        select(func.count())
        .select_from(CHWModuleQuizProgress)
        .where(CHWModuleQuizProgress.chw_id == chw, CHWModuleQuizProgress.module_id == module.id)
    )
    assert int(r.scalar_one()) == 1

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(q1.id),
            "quiz_score_pct": 0.99,
            "outcome": "incorrect",
        }
    )
    r = await db_session.execute(
        select(func.count())
        .select_from(CHWModuleQuizProgress)
        .where(CHWModuleQuizProgress.chw_id == chw, CHWModuleQuizProgress.module_id == module.id)
    )
    assert int(r.scalar_one()) == 1


@pytest.mark.asyncio
@requires_db
async def test_quiz_incorrect_after_full_completion_keeps_module_completion(
    patch_session_local, db_session: AsyncSession
) -> None:
    module = await _make_module(db_session, primary_gap_id=None)
    q1, q2 = await _add_quiz_questions(db_session, module=module, count=2)
    chw = _test_chw_id()

    for quiz in (q1, q2):
        await module_completion_worker.process_module_event_job(
            {
                "event_type": "module_quiz_attempted",
                "event_id": str(uuid4()),
                "chw_id": str(chw),
                "module_id": str(module.id),
                "quiz_id": str(quiz.id),
                "quiz_score_pct": 0.10,
                "outcome": "correct",
            }
        )

    r = await db_session.execute(select(CHWModuleCompletion).where(CHWModuleCompletion.chw_id == chw))
    comp = r.scalar_one()
    assert comp.completed_at is not None
    assert comp.latest_completed_module_id == module.id

    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(module.id),
            "quiz_id": str(q1.id),
            "quiz_score_pct": 0.99,
            "outcome": "incorrect",
        }
    )

    r = await db_session.execute(
        select(func.count())
        .select_from(CHWModuleQuizProgress)
        .where(CHWModuleQuizProgress.chw_id == chw, CHWModuleQuizProgress.module_id == module.id)
    )
    assert int(r.scalar_one()) == 2

    r = await db_session.execute(select(CHWModuleCompletion).where(CHWModuleCompletion.chw_id == chw))
    comp = r.scalar_one()
    assert comp.completed_at is not None
    assert comp.latest_completed_module_id == module.id


# ── version resolution ──────────────────────────────────────────────────


@pytest.mark.asyncio
@requires_db
async def test_module_version_attribution_uses_event_module_id(
    patch_session_local, db_session: AsyncSession
) -> None:
    """The event carries the exact module_id (version-specific row) the SDK
    rendered. The worker attributes the completion to that row, not to the
    family's current_published_module_id — important when a CHW completes
    v1 after v2 has been published (they synced before the new version
    landed)."""
    family = ModuleFamily(module_code=f"VER-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()
    v1 = Module(
        module_family_id=family.id,
        version=1,
        lifecycle_status="deprecated",
        module_type="refresher",
        title_localized={"bn": "v1"},
        domain="hypertension",
        estimated_minutes=5,
        difficulty_level="basic",
    )
    v2 = Module(
        module_family_id=family.id,
        version=2,
        lifecycle_status="published",
        module_type="refresher",
        title_localized={"bn": "v2"},
        domain="hypertension",
        estimated_minutes=5,
        difficulty_level="basic",
    )
    db_session.add_all([v1, v2])
    await db_session.flush()
    family.current_published_module_id = v2.id
    await db_session.flush()
    q1 = (await _add_quiz_questions(db_session, module=v1, count=1))[0]
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "module_quiz_attempted",
            "event_id": str(uuid4()),
            "chw_id": str(chw),
            "module_id": str(v1.id),  # CHW was on v1
            "quiz_id": str(q1.id),
            "quiz_score_pct": 0.10,
            "outcome": "correct",
        }
    )
    r = await db_session.execute(select(CHWModuleCompletion).where(CHWModuleCompletion.chw_id == chw))
    comp = r.scalar_one()
    assert comp.latest_completed_module_id == v1.id  # not v2
