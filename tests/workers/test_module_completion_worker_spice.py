"""module_completion_worker — spice_action_observed gap updates."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.chw_behavioural_gap_state import CHWBehaviouralGapState
from platform_service.workers import module_completion_worker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db
from tests.workers.conftest import _make_gap, _test_chw_id

pytestmark = [pytest.mark.asyncio, requires_db]

# ── spice_action_observed (gap observation) ─────────────────────────────


@pytest.mark.asyncio
@requires_db
async def test_spice_action_observed_records_gap_observation(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "spice_action_observed",
            "event_id": "evt-spice-1",
            "chw_id": str(chw),
            "payload_json": {
                "kind": "assessment_submitted",
                "behavioural_gap_id": str(gap.id),
            },
        }
    )
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.occurrence_count == 1
    assert state.last_observed_at is not None
    assert state.failed_attempts_count == 0


@pytest.mark.asyncio
@requires_db
async def test_spice_action_observed_incorrect_outcome_increments_failed_attempts(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "spice_action_observed",
            "event_id": "evt-spice-incorrect",
            "chw_id": str(chw),
            "outcome": "incorrect",
            "payload_json": {
                "kind": "assessment_submitted",
                "behavioural_gap_id": str(gap.id),
            },
        }
    )
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.occurrence_count == 1
    assert state.failed_attempts_count == 1


@pytest.mark.asyncio
@requires_db
async def test_spice_action_observed_wrong_outcome_nested_in_payload_json(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "spice_action_observed",
            "event_id": "evt-spice-wrong",
            "chw_id": str(chw),
            "payload_json": {
                "kind": "assessment_submitted",
                "behavioural_gap_id": str(gap.id),
                "outcome": "wrong",
            },
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
async def test_spice_action_observed_correct_outcome_does_not_increment_failed_attempts(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "spice_action_observed",
            "event_id": "evt-spice-ok",
            "chw_id": str(chw),
            "outcome": "correct",
            "payload_json": {
                "kind": "assessment_submitted",
                "behavioural_gap_id": str(gap.id),
            },
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


@pytest.mark.asyncio
@requires_db
async def test_spice_action_observed_missing_behavioural_gap_id_no_row(
    patch_session_local, db_session: AsyncSession
) -> None:
    chw = _test_chw_id()
    await module_completion_worker.process_module_event_job(
        {
            "event_type": "spice_action_observed",
            "event_id": "evt-spice-2",
            "chw_id": str(chw),
            "payload_json": {"kind": "assessment_submitted"},
        }
    )
    r = await db_session.execute(select(CHWBehaviouralGapState))
    assert r.first() is None


@pytest.mark.asyncio
@requires_db
async def test_spice_action_observed_second_event_increments_occurrence_count(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    chw = _test_chw_id()
    job = {
        "event_type": "spice_action_observed",
        "chw_id": str(chw),
        "payload_json": {
            "kind": "assessment_submitted",
            "behavioural_gap_id": str(gap.id),
        },
    }
    await module_completion_worker.process_module_event_job({**job, "event_id": "evt-a"})
    await module_completion_worker.process_module_event_job({**job, "event_id": "evt-b"})
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.occurrence_count == 2


@pytest.mark.asyncio
@requires_db
async def test_spice_action_observed_duplicate_uuid_event_id_is_idempotent(
    patch_session_local, db_session: AsyncSession
) -> None:
    gap = await _make_gap(db_session)
    chw = _test_chw_id()
    event_id = str(uuid4())
    job = {
        "event_type": "spice_action_observed",
        "chw_id": str(chw),
        "event_id": event_id,
        "payload_json": {
            "kind": "assessment_submitted",
            "behavioural_gap_id": str(gap.id),
        },
    }
    await module_completion_worker.process_module_event_job(job)
    await module_completion_worker.process_module_event_job(job)
    r = await db_session.execute(
        select(CHWBehaviouralGapState).where(
            CHWBehaviouralGapState.chw_id == chw,
            CHWBehaviouralGapState.behavioural_gap_id == gap.id,
        )
    )
    state = r.scalar_one()
    assert state.occurrence_count == 1
