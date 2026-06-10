"""Tests for LearningPointsRepository (single-table SUM totals)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.chw_learning_point_event import CHWLearningPointEvent
from platform_service.db.repositories.learning_points_repository import LearningPointsRepository
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db


def _chw() -> int:
    return uuid4().int % (10**15) + 1


@pytest.mark.asyncio
@requires_db
async def test_insert_award_row_and_sum_total(db_session: AsyncSession) -> None:
    chw = _chw()
    eid = uuid4()
    repo = LearningPointsRepository(db_session)
    ok = await repo.try_claim_and_increment(
        event_id=eid,
        chw_id=chw,
        tenant_id=None,
        delta=7,
    )
    assert ok is True
    assert await repo.get_total_points(chw_id=chw) == 7


@pytest.mark.asyncio
@requires_db
async def test_same_event_id_second_call_is_no_op(db_session: AsyncSession) -> None:
    chw = _chw()
    eid = uuid4()
    repo = LearningPointsRepository(db_session)
    assert await repo.try_claim_and_increment(event_id=eid, chw_id=chw, tenant_id=None, delta=10) is True
    assert await repo.try_claim_and_increment(event_id=eid, chw_id=chw, tenant_id=None, delta=99) is False
    assert await repo.get_total_points(chw_id=chw) == 10


@pytest.mark.asyncio
@requires_db
async def test_delta_zero_skips_insert(db_session: AsyncSession) -> None:
    chw = _chw()
    eid = uuid4()
    repo = LearningPointsRepository(db_session)
    assert await repo.try_claim_and_increment(event_id=eid, chw_id=chw, tenant_id=None, delta=0) is False
    r = await db_session.execute(select(func.count()).select_from(CHWLearningPointEvent))
    assert int(r.scalar_one()) == 0


@pytest.mark.asyncio
@requires_db
async def test_get_total_points_missing_chw_returns_zero(db_session: AsyncSession) -> None:
    repo = LearningPointsRepository(db_session)
    assert await repo.get_total_points(chw_id=_chw()) == 0


@pytest.mark.asyncio
@requires_db
async def test_multiple_events_sum(db_session: AsyncSession) -> None:
    chw = _chw()
    repo = LearningPointsRepository(db_session)
    e1, e2 = uuid4(), uuid4()
    assert await repo.try_claim_and_increment(event_id=e1, chw_id=chw, tenant_id=None, delta=3) is True
    assert await repo.try_claim_and_increment(event_id=e2, chw_id=chw, tenant_id=None, delta=5) is True
    assert await repo.get_total_points(chw_id=chw) == 8
