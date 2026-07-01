"""ModuleGapRepository — junction table invariants."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.db.models.module_behavioural_gap import ModuleBehaviouralGap
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.module_gap_repository import (
    ModuleGapLinkError,
    ModuleGapRepository,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def _make_gap(session: AsyncSession) -> BehaviouralGap:
    code = f"gap_{uuid4().hex[:8]}"
    gap = BehaviouralGap(
        gap_code=code,
        description=code,
        domain="rmnch",
        detection_rule_jsonb={},
    )
    session.add(gap)
    await session.flush()
    return gap


async def _make_module(session: AsyncSession) -> Module:
    fam = ModuleFamily(module_code=f"fam-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    mod = Module(
        module_family_id=fam.id,
        version=1,
        title_localized={"bn": "t"},
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        module_json={"cards": [{"title": {"bn": "c"}}]},
    )
    session.add(mod)
    await session.flush()
    return mod


async def test_add_primary_link_creates_junction_and_denorm(db_session: AsyncSession) -> None:
    mod = await _make_module(db_session)
    gap = await _make_gap(db_session)
    repo = ModuleGapRepository(db_session)

    await repo.add_primary_link(mod, behavioural_gap_id=gap.id)
    await db_session.refresh(mod)

    assert mod.primary_gap_id == gap.id
    links = await repo.get_links(mod.id)
    assert len(links) == 1
    assert links[0].is_primary is True


async def test_replace_links_multiple_gaps_one_primary(db_session: AsyncSession) -> None:
    mod = await _make_module(db_session)
    gap_a = await _make_gap(db_session)
    gap_b = await _make_gap(db_session)
    repo = ModuleGapRepository(db_session)

    await repo.replace_links(mod.id, gap_ids=[gap_a.id, gap_b.id], primary_gap_id=gap_b.id)
    await db_session.refresh(mod)

    assert mod.primary_gap_id == gap_b.id
    gap_ids = await repo.get_gap_ids(mod.id)
    assert set(gap_ids) == {gap_a.id, gap_b.id}
    primary_rows = (
        (
            await db_session.execute(
                select(ModuleBehaviouralGap).where(
                    ModuleBehaviouralGap.module_id == mod.id,
                    ModuleBehaviouralGap.is_primary.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(primary_rows) == 1
    assert primary_rows[0].behavioural_gap_id == gap_b.id


async def test_replace_links_rejects_primary_not_in_list(db_session: AsyncSession) -> None:
    mod = await _make_module(db_session)
    gap = await _make_gap(db_session)
    repo = ModuleGapRepository(db_session)

    with pytest.raises(ModuleGapLinkError):
        await repo.replace_links(mod.id, gap_ids=[gap.id], primary_gap_id=uuid4())


async def test_replace_secondary_links_keeps_primary(db_session: AsyncSession) -> None:
    mod = await _make_module(db_session)
    primary = await _make_gap(db_session)
    secondary = await _make_gap(db_session)
    repo = ModuleGapRepository(db_session)

    await repo.add_primary_link(mod, behavioural_gap_id=primary.id)
    await repo.replace_secondary_links(
        mod.id,
        secondary_gap_ids=[secondary.id],
        primary_gap_id=primary.id,
    )
    await db_session.refresh(mod)

    assert mod.primary_gap_id == primary.id
    assert set(await repo.get_gap_ids(mod.id)) == {primary.id, secondary.id}
    primary_rows = (
        (
            await db_session.execute(
                select(ModuleBehaviouralGap).where(
                    ModuleBehaviouralGap.module_id == mod.id,
                    ModuleBehaviouralGap.is_primary.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(primary_rows) == 1
    assert primary_rows[0].behavioural_gap_id == primary.id


async def test_copy_links_preserves_all_gaps(db_session: AsyncSession) -> None:
    mod_from = await _make_module(db_session)
    mod_to = await _make_module(db_session)
    gap_a = await _make_gap(db_session)
    gap_b = await _make_gap(db_session)
    repo = ModuleGapRepository(db_session)

    await repo.replace_links(mod_from.id, gap_ids=[gap_a.id, gap_b.id], primary_gap_id=gap_a.id)
    await repo.copy_links(mod_from.id, mod_to.id)
    await db_session.refresh(mod_to)

    assert set(await repo.get_gap_ids(mod_to.id)) == {gap_a.id, gap_b.id}
    assert mod_to.primary_gap_id == gap_a.id
