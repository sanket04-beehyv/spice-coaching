"""W-8 — module_selector integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from platform_service.db.models.chw_module_completion import CHWModuleCompletion
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.trigger_repository import TriggerRepository
from platform_service.services.module_selector import ModuleSelector
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db


def _test_chw_id() -> int:
    return uuid4().int % (10**15) + 1


async def _make_family(session: AsyncSession) -> ModuleFamily:
    family = ModuleFamily(module_code=f"SEL-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    return family


async def _make_trigger_with_bindings(
    session: AsyncSession,
    *,
    trigger_kind: str = "gap",
    trigger_code: str | None = None,
    bindings: list[tuple[ModuleFamily, int]] | None = None,
):
    repo = TriggerRepository(session)
    trigger = await repo.create_trigger(
        trigger_kind=trigger_kind,
        trigger_code=trigger_code or f"trig_{uuid4().hex[:8]}",
        predicate_jsonb={"behavioural_gap_code": "x"} if trigger_kind == "gap" else {},
    )
    for family, weight in bindings or []:
        await repo.bind_module_to_trigger(
            module_family_id=family.id,
            trigger_definition_id=trigger.id,
            priority_weight=weight,
        )
    return trigger


# ── Tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@requires_db
async def test_no_fired_triggers_returns_empty(db_session: AsyncSession) -> None:
    selector = ModuleSelector(db_session)
    out = await selector.select_modules_for_chw(chw_id=_test_chw_id(), fired_trigger_codes=[])
    assert out == []


@pytest.mark.asyncio
@requires_db
async def test_workflow_event_with_no_binding_is_noop(db_session: AsyncSession) -> None:
    """Edge case 6: workflow event fires but no module bound → no-op."""
    trigger = await _make_trigger_with_bindings(db_session, trigger_kind="workflow_event", bindings=[])
    selector = ModuleSelector(db_session)
    out = await selector.select_modules_for_chw(
        chw_id=_test_chw_id(), fired_trigger_codes=[trigger.trigger_code]
    )
    assert out == []


@pytest.mark.asyncio
@requires_db
async def test_returns_highest_priority_module(db_session: AsyncSession) -> None:
    """Edge case 3: two modules bound to same gap → highest weight wins."""
    fam_high = await _make_family(db_session)
    fam_low = await _make_family(db_session)
    trigger = await _make_trigger_with_bindings(db_session, bindings=[(fam_low, 5), (fam_high, 10)])
    selector = ModuleSelector(db_session)
    out = await selector.select_modules_for_chw(
        chw_id=_test_chw_id(), fired_trigger_codes=[trigger.trigger_code]
    )
    assert [c.module_family_id for c in out] == [fam_high.id, fam_low.id]
    assert [c.priority_weight for c in out] == [10, 5]


@pytest.mark.asyncio
@requires_db
async def test_skips_module_in_periodic_refresh_window(db_session: AsyncSession) -> None:
    """Edge case 4: weight-10 completed 30 days ago, refresh_due in future → return weight-5."""
    chw_id = _test_chw_id()
    fam_high = await _make_family(db_session)
    fam_low = await _make_family(db_session)
    trigger = await _make_trigger_with_bindings(db_session, bindings=[(fam_high, 10), (fam_low, 5)])
    # CHW passed fam_high 30 days ago and reinforcement is due in 60 days.
    db_session.add(
        CHWModuleCompletion(
            chw_id=chw_id,
            module_family_id=fam_high.id,
            latest_attempt_passed=True,
            completed_at=datetime.now(UTC) - timedelta(days=30),
            reinforcement_due_at=datetime.now(UTC) + timedelta(days=60),
        )
    )
    await db_session.flush()
    selector = ModuleSelector(db_session)
    out = await selector.select_modules_for_chw(chw_id=chw_id, fired_trigger_codes=[trigger.trigger_code])
    assert [c.module_family_id for c in out] == [fam_low.id]


@pytest.mark.asyncio
@requires_db
async def test_module_completion_past_refresh_due_resurfaces(
    db_session: AsyncSession,
) -> None:
    """Once reinforcement_due_at passes, the module surfaces again."""
    chw_id = _test_chw_id()
    fam = await _make_family(db_session)
    trigger = await _make_trigger_with_bindings(db_session, bindings=[(fam, 10)])
    db_session.add(
        CHWModuleCompletion(
            chw_id=chw_id,
            module_family_id=fam.id,
            latest_attempt_passed=True,
            completed_at=datetime.now(UTC) - timedelta(days=120),
            reinforcement_due_at=datetime.now(UTC) - timedelta(days=1),  # past due
        )
    )
    await db_session.flush()
    selector = ModuleSelector(db_session)
    out = await selector.select_modules_for_chw(chw_id=chw_id, fired_trigger_codes=[trigger.trigger_code])
    assert [c.module_family_id for c in out] == [fam.id]


@pytest.mark.asyncio
@requires_db
async def test_failed_attempt_does_not_suppress(db_session: AsyncSession) -> None:
    """A CHW who attempted but did NOT pass should still see the module."""
    chw_id = _test_chw_id()
    fam = await _make_family(db_session)
    trigger = await _make_trigger_with_bindings(db_session, bindings=[(fam, 10)])
    db_session.add(
        CHWModuleCompletion(
            chw_id=chw_id,
            module_family_id=fam.id,
            latest_attempt_passed=False,
            latest_attempt_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db_session.flush()
    selector = ModuleSelector(db_session)
    out = await selector.select_modules_for_chw(chw_id=chw_id, fired_trigger_codes=[trigger.trigger_code])
    assert [c.module_family_id for c in out] == [fam.id]


@pytest.mark.asyncio
@requires_db
async def test_inactive_trigger_filtered_out(db_session: AsyncSession) -> None:
    """Edge case 10: deprecated trigger should not surface modules."""
    fam = await _make_family(db_session)
    trigger = await _make_trigger_with_bindings(db_session, bindings=[(fam, 10)])
    repo = TriggerRepository(db_session)
    await repo.deprecate_trigger(trigger.id)
    selector = ModuleSelector(db_session)
    out = await selector.select_modules_for_chw(
        chw_id=_test_chw_id(), fired_trigger_codes=[trigger.trigger_code]
    )
    assert out == []


@pytest.mark.asyncio
@requires_db
async def test_module_reachable_from_two_triggers_dedupes_by_higher_weight(
    db_session: AsyncSession,
) -> None:
    fam = await _make_family(db_session)
    t1 = await _make_trigger_with_bindings(db_session, bindings=[(fam, 5)])
    t2 = await _make_trigger_with_bindings(db_session, bindings=[(fam, 15)])
    selector = ModuleSelector(db_session)
    out = await selector.select_modules_for_chw(
        chw_id=_test_chw_id(),
        fired_trigger_codes=[t1.trigger_code, t2.trigger_code],
    )
    assert len(out) == 1
    assert out[0].module_family_id == fam.id
    assert out[0].priority_weight == 15
