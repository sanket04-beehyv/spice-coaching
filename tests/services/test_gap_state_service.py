"""W-8 — gap_state_service integration tests.

Covers the telemetry → state → trigger evaluation flow plus the
supervisor-escalation rule (Pipeline §12A: ≥3 failed attempts within 30 days).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from platform_service.config import get_settings
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.trigger_repository import TriggerRepository
from platform_service.services.gap_state_service import GapStateService
from platform_service.services.module_selector import ModuleSelector
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db


def _test_chw_id() -> int:
    return uuid4().int % (10**15) + 1


async def _make_gap(session: AsyncSession) -> BehaviouralGap:
    code = f"gap_state_{uuid4().hex[:8]}"
    gap = BehaviouralGap(
        gap_code=code,
        description=code,
        domain="hypertension",
        detection_rule_jsonb={},
    )
    session.add(gap)
    await session.flush()
    return gap


# ── record_observation ─────────────────────────────────────────────────


@pytest.mark.asyncio
@requires_db
async def test_first_observation_creates_state_at_count_1(db_session: AsyncSession) -> None:
    gap = await _make_gap(db_session)
    chw_id = _test_chw_id()
    svc = GapStateService(db_session)
    outcome = await svc.record_observation(chw_id=chw_id, behavioural_gap_id=gap.id)
    assert outcome.state.occurrence_count == 1
    assert outcome.state.first_observed_at is not None
    assert outcome.state.last_observed_at == outcome.state.first_observed_at


@pytest.mark.asyncio
@requires_db
async def test_observation_within_window_increments(db_session: AsyncSession) -> None:
    gap = await _make_gap(db_session)
    chw_id = _test_chw_id()
    svc = GapStateService(db_session)
    predicate = {"behavioural_gap_code": gap.gap_code, "window_days": 14}
    await svc.record_observation(chw_id=chw_id, behavioural_gap_id=gap.id, predicate=predicate)
    out2 = await svc.record_observation(chw_id=chw_id, behavioural_gap_id=gap.id, predicate=predicate)
    assert out2.state.occurrence_count == 2


@pytest.mark.asyncio
@requires_db
async def test_observation_outside_window_resets_counter(db_session: AsyncSession) -> None:
    """Edge case #2: 2nd occurrence on day 15 (window=14) → counter resets to 1."""
    gap = await _make_gap(db_session)
    chw_id = _test_chw_id()
    svc = GapStateService(db_session)
    predicate = {
        "behavioural_gap_code": gap.gap_code,
        "occurrences_threshold": 2,
        "window_days": 14,
    }
    base_time = datetime.now(UTC) - timedelta(days=20)
    await svc.record_observation(chw_id=chw_id, behavioural_gap_id=gap.id, predicate=predicate, now=base_time)
    # 20 days later (= now): the previous observation is outside the 14-day window.
    out = await svc.record_observation(chw_id=chw_id, behavioural_gap_id=gap.id, predicate=predicate)
    assert out.state.occurrence_count == 1
    assert out.decision is not None
    assert out.decision.fired is False
    assert out.decision.reason == "below_threshold"


@pytest.mark.asyncio
@requires_db
async def test_threshold_crossed_within_window_fires_trigger(
    db_session: AsyncSession,
) -> None:
    gap = await _make_gap(db_session)
    chw_id = _test_chw_id()
    svc = GapStateService(db_session)
    predicate = {
        "behavioural_gap_code": gap.gap_code,
        "occurrences_threshold": 2,
        "window_days": 14,
    }
    await svc.record_observation(chw_id=chw_id, behavioural_gap_id=gap.id, predicate=predicate)
    out = await svc.record_observation(chw_id=chw_id, behavioural_gap_id=gap.id, predicate=predicate)
    assert out.state.occurrence_count == 2
    assert out.decision is not None
    assert out.decision.fired is True
    assert out.decision.reason == "fired"


@pytest.mark.asyncio
@requires_db
async def test_observation_without_predicate_returns_no_decision(
    db_session: AsyncSession,
) -> None:
    gap = await _make_gap(db_session)
    svc = GapStateService(db_session)
    out = await svc.record_observation(chw_id=_test_chw_id(), behavioural_gap_id=gap.id, predicate=None)
    assert out.decision is None


# ── failed_attempt + escalation ────────────────────────────────────────


@pytest.mark.asyncio
@requires_db
async def test_failed_attempt_increments_and_escalates_at_threshold(
    db_session: AsyncSession,
) -> None:
    settings = get_settings()
    threshold = settings.quiz_failure_escalation_count
    gap = await _make_gap(db_session)
    chw_id = _test_chw_id()
    svc = GapStateService(db_session)
    state = None
    for _ in range(threshold):
        state = await svc.record_failed_attempt(chw_id=chw_id, behavioural_gap_id=gap.id)
    assert state is not None
    assert state.failed_attempts_count == threshold
    assert state.escalated_to_supervisor is True


@pytest.mark.asyncio
@requires_db
async def test_failed_attempt_outside_window_resets(db_session: AsyncSession) -> None:
    settings = get_settings()
    gap = await _make_gap(db_session)
    chw_id = _test_chw_id()
    svc = GapStateService(db_session)
    long_ago = datetime.now(UTC) - timedelta(days=settings.quiz_failure_escalation_window_days + 5)
    state = await svc.record_failed_attempt(chw_id=chw_id, behavioural_gap_id=gap.id, now=long_ago)
    assert state.failed_attempts_count == 1
    state = await svc.record_failed_attempt(chw_id=chw_id, behavioural_gap_id=gap.id)
    # Previous failure was outside window → counter resets.
    assert state.failed_attempts_count == 1
    assert state.escalated_to_supervisor is False


@pytest.mark.asyncio
@requires_db
async def test_reset_after_pass_clears_failures_and_de_escalates(
    db_session: AsyncSession,
) -> None:
    settings = get_settings()
    gap = await _make_gap(db_session)
    chw_id = _test_chw_id()
    svc = GapStateService(db_session)
    for _ in range(settings.quiz_failure_escalation_count):
        await svc.record_failed_attempt(chw_id=chw_id, behavioural_gap_id=gap.id)
    state = await svc.reset_after_pass(chw_id=chw_id, behavioural_gap_id=gap.id)
    assert state is not None
    assert state.failed_attempts_count == 0
    assert state.escalated_to_supervisor is False
    assert state.last_reinforced_at is not None


# ── Integration: trigger fires + module selector picks the module ──────


@pytest.mark.asyncio
@requires_db
async def test_telemetry_to_module_surface_e2e(db_session: AsyncSession) -> None:
    """End-to-end: 2 observations cross threshold → evaluator says fire →
    module_selector returns the module bound to the trigger."""
    gap = await _make_gap(db_session)
    family = ModuleFamily(module_code=f"E2E-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()

    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": "e2e gap module"},
        domain="iccm",
        module_type="refresher",
        lifecycle_status="published",
        module_json={"cards": []},
    )
    db_session.add(module)
    await db_session.flush()

    trigger_repo = TriggerRepository(db_session)
    trigger = await trigger_repo.create_trigger(
        trigger_kind="gap",
        trigger_code=f"e2e_trig_{uuid4().hex[:8]}",
        predicate_jsonb={
            "behavioural_gap_code": gap.gap_code,
            "occurrences_threshold": 2,
            "window_days": 14,
        },
    )
    await trigger_repo.bind_module_to_trigger(
        module_id=module.id,
        trigger_definition_id=trigger.id,
        priority_weight=10,
    )

    chw_id = _test_chw_id()
    state_svc = GapStateService(db_session)
    await state_svc.record_observation(
        chw_id=chw_id,
        behavioural_gap_id=gap.id,
        predicate=trigger.predicate_jsonb,
    )
    out = await state_svc.record_observation(
        chw_id=chw_id,
        behavioural_gap_id=gap.id,
        predicate=trigger.predicate_jsonb,
    )
    assert out.decision.fired is True

    selector = ModuleSelector(db_session)
    selected = await selector.select_modules_for_chw(
        chw_id=chw_id,
        fired_trigger_codes=[trigger.trigger_code],
    )
    assert [c.module_family_id for c in selected] == [family.id]
