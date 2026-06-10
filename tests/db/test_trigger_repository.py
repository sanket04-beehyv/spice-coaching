"""W-8 — TriggerRepository integration tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.trigger_repository import TriggerRepository
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db


@pytest.mark.asyncio
@requires_db
async def test_create_then_get_trigger_by_code(db_session: AsyncSession) -> None:
    repo = TriggerRepository(db_session)
    code = f"trig_{uuid4().hex[:8]}"
    created = await repo.create_trigger(
        trigger_kind="gap",
        trigger_code=code,
        predicate_jsonb={"behavioural_gap_code": "x"},
    )
    fetched = await repo.get_trigger_by_code(code)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.predicate_jsonb == {"behavioural_gap_code": "x"}
    assert fetched.status == "active"


@pytest.mark.asyncio
@requires_db
async def test_duplicate_trigger_code_rejected(db_session: AsyncSession) -> None:
    repo = TriggerRepository(db_session)
    code = f"dup_{uuid4().hex[:8]}"
    await repo.create_trigger(
        trigger_kind="gap", trigger_code=code, predicate_jsonb={"behavioural_gap_code": "x"}
    )
    with pytest.raises(IntegrityError):
        await repo.create_trigger(
            trigger_kind="gap", trigger_code=code, predicate_jsonb={"behavioural_gap_code": "y"}
        )


@pytest.mark.asyncio
@requires_db
async def test_list_active_triggers_filters_by_kind(db_session: AsyncSession) -> None:
    repo = TriggerRepository(db_session)
    code1 = f"gapfilter_{uuid4().hex[:8]}"
    code2 = f"wffilter_{uuid4().hex[:8]}"
    await repo.create_trigger(
        trigger_kind="gap", trigger_code=code1, predicate_jsonb={"behavioural_gap_code": "x"}
    )
    await repo.create_trigger(
        trigger_kind="workflow_event",
        trigger_code=code2,
        predicate_jsonb={"spice_event_code": "x"},
    )
    gaps = await repo.list_active_triggers(trigger_kind="gap")
    codes = [t.trigger_code for t in gaps]
    assert code1 in codes
    assert code2 not in codes


@pytest.mark.asyncio
@requires_db
async def test_deprecate_trigger_hides_from_active_list(db_session: AsyncSession) -> None:
    repo = TriggerRepository(db_session)
    code = f"deprec_{uuid4().hex[:8]}"
    trig = await repo.create_trigger(
        trigger_kind="gap", trigger_code=code, predicate_jsonb={"behavioural_gap_code": "x"}
    )
    await repo.deprecate_trigger(trig.id)
    fetched = await repo.get_trigger(trig.id)
    assert fetched.status == "deprecated"
    actives = await repo.list_active_triggers(trigger_kind="gap")
    assert trig.id not in [t.id for t in actives]


@pytest.mark.asyncio
@requires_db
async def test_bind_module_to_trigger_creates_row(db_session: AsyncSession) -> None:
    repo = TriggerRepository(db_session)
    family = ModuleFamily(module_code=f"BIND-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()
    trigger = await repo.create_trigger(
        trigger_kind="gap",
        trigger_code=f"bindtrig_{uuid4().hex[:8]}",
        predicate_jsonb={"behavioural_gap_code": "x"},
    )
    binding = await repo.bind_module_to_trigger(
        module_family_id=family.id,
        trigger_definition_id=trigger.id,
        priority_weight=20,
    )
    assert binding.priority_weight == 20
    assert binding.relationship == "primary"
    bindings = await repo.list_bindings_for_trigger(trigger.id)
    assert [b.id for b in bindings] == [binding.id]


@pytest.mark.asyncio
@requires_db
async def test_bind_invalid_relationship_rejected(db_session: AsyncSession) -> None:
    repo = TriggerRepository(db_session)
    family = ModuleFamily(module_code=f"BR-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()
    trigger = await repo.create_trigger(
        trigger_kind="gap",
        trigger_code=f"br_{uuid4().hex[:8]}",
        predicate_jsonb={"behavioural_gap_code": "x"},
    )
    with pytest.raises(ValueError, match="relationship"):
        await repo.bind_module_to_trigger(
            module_family_id=family.id,
            trigger_definition_id=trigger.id,
            relationship="tertiary",
        )


@pytest.mark.asyncio
@requires_db
async def test_duplicate_binding_rejected(db_session: AsyncSession) -> None:
    repo = TriggerRepository(db_session)
    family = ModuleFamily(module_code=f"DB-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()
    trigger = await repo.create_trigger(
        trigger_kind="gap",
        trigger_code=f"db_{uuid4().hex[:8]}",
        predicate_jsonb={"behavioural_gap_code": "x"},
    )
    await repo.bind_module_to_trigger(module_family_id=family.id, trigger_definition_id=trigger.id)
    with pytest.raises(IntegrityError):
        await repo.bind_module_to_trigger(module_family_id=family.id, trigger_definition_id=trigger.id)


@pytest.mark.asyncio
@requires_db
async def test_list_active_bindings_for_trigger_codes_skips_deprecated(
    db_session: AsyncSession,
) -> None:
    repo = TriggerRepository(db_session)
    family = ModuleFamily(module_code=f"AC-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()
    code = f"ac_{uuid4().hex[:8]}"
    trigger = await repo.create_trigger(
        trigger_kind="gap", trigger_code=code, predicate_jsonb={"behavioural_gap_code": "x"}
    )
    await repo.bind_module_to_trigger(
        module_family_id=family.id, trigger_definition_id=trigger.id, priority_weight=7
    )
    pairs = await repo.list_active_bindings_for_trigger_codes([code])
    assert len(pairs) == 1

    await repo.deprecate_trigger(trigger.id)
    pairs = await repo.list_active_bindings_for_trigger_codes([code])
    assert pairs == []
