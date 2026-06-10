"""ModuleSuggestionService — gap-based suggestions + fallback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.chw_behavioural_gap_state import CHWBehaviouralGapState
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.services.module_suggestion_service import ModuleSuggestionService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


def _test_chw_id() -> int:
    return uuid4().int % (10**15) + 1


async def _make_gap(
    session: AsyncSession,
    *,
    code: str | None = None,
    severity_default: str = "moderate",
) -> BehaviouralGap:
    c = code or f"gap_{uuid4().hex[:8]}"
    gap = BehaviouralGap(
        gap_code=c,
        description=c,
        domain="rmnch",
        severity_default=severity_default,
        detection_rule_jsonb={},
    )
    session.add(gap)
    await session.flush()
    return gap


async def _make_family(session: AsyncSession) -> ModuleFamily:
    fam = ModuleFamily(module_code=f"MSF-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    return fam


async def _make_published_module(
    session: AsyncSession,
    *,
    family: ModuleFamily,
    tenant_id: UUID | None,
    primary_gap_id: UUID | None = None,
    created_at: datetime | None = None,
    set_family_pointer: bool = True,
) -> Module:
    now = datetime.now(UTC)
    mod = Module(
        module_family_id=family.id,
        version=1,
        title_bn="t",
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        tenant_id=tenant_id,
        primary_gap_id=primary_gap_id,
        module_json={"cards": [{"title_bn": "c"}]},
        published_at=now,
        created_at=created_at or now,
    )
    session.add(mod)
    await session.flush()
    if primary_gap_id is not None:
        await ModuleGapRepository(session).add_primary_link(mod, behavioural_gap_id=primary_gap_id)
    if set_family_pointer:
        family.current_published_module_id = mod.id
        await session.flush()
    return mod


async def test_fallback_orders_by_primary_gap_severity_default(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid4()
    chw_id = _test_chw_id()
    base = datetime.now(UTC) - timedelta(days=10)
    gap_low = await _make_gap(db_session, severity_default="low")
    gap_high = await _make_gap(db_session, severity_default="high")

    fam_lo = await _make_family(db_session)
    fam_hi = await _make_family(db_session)
    mod_lo = await _make_published_module(
        db_session,
        family=fam_lo,
        tenant_id=tenant_id,
        primary_gap_id=gap_low.id,
        created_at=base + timedelta(hours=10),
    )
    mod_hi = await _make_published_module(
        db_session,
        family=fam_hi,
        tenant_id=tenant_id,
        primary_gap_id=gap_high.id,
        created_at=base + timedelta(hours=1),
    )

    svc = ModuleSuggestionService(db_session)
    out = await svc.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
    assert len(out) == 2
    assert all(x.source == "fallback" for x in out)
    assert out[0].module_id == mod_hi.id
    assert out[1].module_id == mod_lo.id


async def test_fallback_when_no_gap_states(db_session: AsyncSession) -> None:
    tenant_id = uuid4()
    chw_id = _test_chw_id()
    base = datetime.now(UTC) - timedelta(days=10)
    modules: list[Module] = []
    for i in range(6):
        fam = await _make_family(db_session)
        m = await _make_published_module(
            db_session,
            family=fam,
            tenant_id=tenant_id,
            primary_gap_id=None,
            created_at=base + timedelta(hours=i),
        )
        modules.append(m)

    svc = ModuleSuggestionService(db_session)
    out = await svc.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
    assert len(out) == 5
    assert all(x.source == "fallback" for x in out)
    assert all(x.behavioural_gap_id is None for x in out)
    expected_ids = {modules[5].id, modules[4].id, modules[3].id, modules[2].id, modules[1].id}
    assert {x.module_id for x in out} == expected_ids


async def test_gap_path_orders_by_severity_then_picks_one_module_per_gap(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid4()
    chw_id = _test_chw_id()
    gap_low = await _make_gap(db_session, severity_default="low")
    gap_high = await _make_gap(db_session, severity_default="high")

    db_session.add(
        CHWBehaviouralGapState(
            chw_id=chw_id,
            behavioural_gap_id=gap_low.id,
            tenant_id=tenant_id,
            status="active",
            severity_current="moderate",
            occurrence_count=99,
        )
    )
    db_session.add(
        CHWBehaviouralGapState(
            chw_id=chw_id,
            behavioural_gap_id=gap_high.id,
            tenant_id=tenant_id,
            status="active",
            severity_current="moderate",
            occurrence_count=1,
        )
    )
    await db_session.flush()

    fam_lo = await _make_family(db_session)
    fam_hi = await _make_family(db_session)
    mod_lo = await _make_published_module(
        db_session, family=fam_lo, tenant_id=tenant_id, primary_gap_id=gap_low.id
    )
    mod_hi = await _make_published_module(
        db_session, family=fam_hi, tenant_id=tenant_id, primary_gap_id=gap_high.id
    )

    svc = ModuleSuggestionService(db_session)
    out = await svc.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
    assert len(out) == 2
    assert out[0].source == "gap" and out[0].module_id == mod_hi.id
    assert out[0].behavioural_gap_id == gap_high.id
    assert out[1].module_id == mod_lo.id
    assert out[1].behavioural_gap_id == gap_low.id


async def test_gap_path_orders_by_severity_default_when_severity_current_uniform(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid4()
    chw_id = _test_chw_id()
    gap_low = await _make_gap(db_session, severity_default="low")
    gap_high = await _make_gap(db_session, severity_default="high")

    for gap in (gap_low, gap_high):
        db_session.add(
            CHWBehaviouralGapState(
                chw_id=chw_id,
                behavioural_gap_id=gap.id,
                tenant_id=tenant_id,
                status="active",
                severity_current="moderate",
                occurrence_count=1,
            )
        )
    await db_session.flush()

    fam_lo = await _make_family(db_session)
    fam_hi = await _make_family(db_session)
    mod_lo = await _make_published_module(
        db_session, family=fam_lo, tenant_id=tenant_id, primary_gap_id=gap_low.id
    )
    mod_hi = await _make_published_module(
        db_session, family=fam_hi, tenant_id=tenant_id, primary_gap_id=gap_high.id
    )

    svc = ModuleSuggestionService(db_session)
    out = await svc.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
    assert len(out) == 2
    assert out[0].module_id == mod_hi.id
    assert out[0].behavioural_gap_id == gap_high.id
    assert out[1].module_id == mod_lo.id
    assert out[1].behavioural_gap_id == gap_low.id


async def test_fallback_when_gaps_exist_but_no_matching_modules(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid4()
    chw_id = _test_chw_id()
    gap = await _make_gap(db_session)
    db_session.add(
        CHWBehaviouralGapState(
            chw_id=chw_id,
            behavioural_gap_id=gap.id,
            tenant_id=tenant_id,
            status="active",
            severity_current="moderate",
            occurrence_count=1,
        )
    )
    await db_session.flush()

    fam = await _make_family(db_session)
    await _make_published_module(
        db_session,
        family=fam,
        tenant_id=tenant_id,
        primary_gap_id=None,
    )

    svc = ModuleSuggestionService(db_session)
    out = await svc.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
    assert len(out) == 1
    assert out[0].source == "fallback"


async def test_excludes_module_wrong_tenant(db_session: AsyncSession) -> None:
    tenant_id = uuid4()
    other_tenant = uuid4()
    chw_id = _test_chw_id()
    gap = await _make_gap(db_session)
    db_session.add(
        CHWBehaviouralGapState(
            chw_id=chw_id,
            behavioural_gap_id=gap.id,
            tenant_id=tenant_id,
            status="active",
            severity_current="moderate",
            occurrence_count=1,
        )
    )
    await db_session.flush()

    fam_wrong = await _make_family(db_session)
    await _make_published_module(
        db_session,
        family=fam_wrong,
        tenant_id=other_tenant,
        primary_gap_id=gap.id,
    )
    fam_ok = await _make_family(db_session)
    mod_ok = await _make_published_module(
        db_session,
        family=fam_ok,
        tenant_id=tenant_id,
        primary_gap_id=gap.id,
    )

    svc = ModuleSuggestionService(db_session)
    out = await svc.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
    assert len(out) == 1
    assert out[0].module_id == mod_ok.id
    assert out[0].source == "gap"


async def test_prefers_current_published_pointer_over_old_version(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid4()
    chw_id = _test_chw_id()
    gap = await _make_gap(db_session)
    db_session.add(
        CHWBehaviouralGapState(
            chw_id=chw_id,
            behavioural_gap_id=gap.id,
            tenant_id=tenant_id,
            status="active",
            severity_current="moderate",
            occurrence_count=1,
        )
    )
    await db_session.flush()

    fam = await _make_family(db_session)
    v1 = Module(
        module_family_id=fam.id,
        version=1,
        title_bn="v1",
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        tenant_id=tenant_id,
        primary_gap_id=gap.id,
        module_json={"cards": [{"title_bn": "c"}]},
        published_at=datetime.now(UTC),
    )
    db_session.add(v1)
    await db_session.flush()
    v2 = Module(
        module_family_id=fam.id,
        version=2,
        title_bn="v2",
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        tenant_id=tenant_id,
        primary_gap_id=gap.id,
        module_json={"cards": [{"title_bn": "c"}]},
        published_at=datetime.now(UTC),
    )
    db_session.add(v2)
    await db_session.flush()
    fam.current_published_module_id = v1.id
    await db_session.flush()

    svc = ModuleSuggestionService(db_session)
    out = await svc.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
    assert len(out) == 1
    assert out[0].module_id == v1.id


async def test_caps_at_five_modules(db_session: AsyncSession) -> None:
    tenant_id = uuid4()
    chw_id = _test_chw_id()
    gap_ids: list[UUID] = []
    for _ in range(6):
        gap = await _make_gap(db_session)
        gap_ids.append(gap.id)
        db_session.add(
            CHWBehaviouralGapState(
                chw_id=chw_id,
                behavioural_gap_id=gap.id,
                tenant_id=tenant_id,
                status="active",
                severity_current="moderate",
                occurrence_count=1,
            )
        )
    await db_session.flush()

    for gid in gap_ids:
        fam = await _make_family(db_session)
        await _make_published_module(
            db_session,
            family=fam,
            tenant_id=tenant_id,
            primary_gap_id=gid,
        )

    svc = ModuleSuggestionService(db_session)
    out = await svc.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
    assert len(out) == 5
    assert all(x.source == "gap" for x in out)


async def test_module_with_multiple_gaps_matches_either_active_gap(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid4()
    chw_id = _test_chw_id()
    gap_a = await _make_gap(db_session, severity_default="low")
    gap_b = await _make_gap(db_session, severity_default="high")

    db_session.add(
        CHWBehaviouralGapState(
            chw_id=chw_id,
            behavioural_gap_id=gap_b.id,
            tenant_id=tenant_id,
            status="active",
            severity_current="moderate",
            occurrence_count=1,
        )
    )
    await db_session.flush()

    fam = await _make_family(db_session)
    mod = await _make_published_module(
        db_session,
        family=fam,
        tenant_id=tenant_id,
        primary_gap_id=gap_a.id,
    )
    await ModuleGapRepository(db_session).replace_links(
        mod.id,
        gap_ids=[gap_a.id, gap_b.id],
        primary_gap_id=gap_a.id,
    )

    svc = ModuleSuggestionService(db_session)
    out = await svc.suggest_for_chw(chw_id=chw_id, tenant_id=tenant_id)
    assert len(out) == 1
    assert out[0].module_id == mod.id
    assert out[0].behavioural_gap_id == gap_b.id
    assert out[0].source == "gap"
