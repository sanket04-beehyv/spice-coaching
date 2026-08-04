"""training_request_event_worker — MODULE_REQUESTED → TrainingRequestService."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from platform_service.db.models.chw_module_assignment import CHWModuleAssignment
from platform_service.db.models.chw_training_request import CHWTrainingRequest
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.workers import training_request_event_worker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db, truncate_tables

pytestmark = [pytest.mark.asyncio, requires_db]


def _test_chw_id() -> int:
    return uuid4().int % (10**15) + 1


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data(db_session: AsyncSession) -> AsyncIterator[None]:
    await truncate_tables(
        db_session,
        "chw_training_request, chw_module_assignment, module, module_family",
    )
    yield


@pytest.fixture
def patch_session_local(db_session: AsyncSession):
    @asynccontextmanager
    async def _factory():
        original_commit = db_session.commit
        original_rollback = db_session.rollback

        async def _commit_as_flush() -> None:
            await db_session.flush()

        async def _rollback_noop() -> None:
            # Worker rollbacks must not undo prior flushed work in the shared
            # test session (e.g. first create before a duplicate no-op).
            return None

        db_session.commit = _commit_as_flush  # type: ignore[method-assign]
        db_session.rollback = _rollback_noop  # type: ignore[method-assign]
        try:
            yield db_session
        finally:
            db_session.commit = original_commit  # type: ignore[method-assign]
            db_session.rollback = original_rollback  # type: ignore[method-assign]

    with patch.object(training_request_event_worker, "SessionLocal", _factory):
        yield


async def _seed_published_module(
    session: AsyncSession,
    *,
    tenant_id: UUID | None = None,
    chatbot_faqs_only: bool = False,
    lifecycle_status: str = "published",
) -> Module:
    fam = ModuleFamily(module_code=f"TR-{uuid4().hex[:8]}")
    session.add(fam)
    await session.flush()
    module = Module(
        module_family_id=fam.id,
        version=1,
        title_localized={"bn": "গর্ভাবস্থার ঝুঁকি", "en": "Pregnancy Risk Assessment"},
        domain="clinical",
        module_type="refresher",
        lifecycle_status=lifecycle_status,
        clinically_reviewed=True,
        module_json={"cards": [{"title": {"bn": "Card"}}]},
        published_at=datetime.now(UTC) if lifecycle_status == "published" else None,
        tenant_id=tenant_id,
        chatbot_faqs_only=chatbot_faqs_only,
    )
    session.add(module)
    await session.flush()
    if lifecycle_status == "published":
        fam.current_published_module_id = module.id
    await session.commit()
    return module


async def test_published_module_creates_request_and_assignment(
    patch_session_local, db_session: AsyncSession
) -> None:
    chw = _test_chw_id()
    module = await _seed_published_module(db_session)
    await training_request_event_worker.process_training_request_event_job(
        {
            "event_type": "module_requested",
            "event_id": str(uuid4()),
            "chw_id": chw,
            "module_id": str(module.id),
            "reason": "Need refresher before field visits",
        }
    )
    row = (
        await db_session.execute(select(CHWTrainingRequest).where(CHWTrainingRequest.chw_id == chw))
    ).scalar_one()
    assert row.module_id == module.id
    assert row.reason == "Need refresher before field visits"
    assignment = (
        await db_session.execute(
            select(CHWModuleAssignment).where(
                CHWModuleAssignment.module_id == module.id,
                CHWModuleAssignment.user_id == chw,
            )
        )
    ).scalar_one()
    assert assignment.assignment_type == "individual"


async def test_free_text_name_creates_request_without_module_id(
    patch_session_local, db_session: AsyncSession
) -> None:
    chw = _test_chw_id()
    await training_request_event_worker.process_training_request_event_job(
        {
            "event_type": "module_requested",
            "event_id": str(uuid4()),
            "chw_id": chw,
            "requested_module_name": "Diabetes Counseling Refresh",
            "reason": "Need support",
        }
    )
    row = (
        await db_session.execute(select(CHWTrainingRequest).where(CHWTrainingRequest.chw_id == chw))
    ).scalar_one()
    assert row.module_id is None
    assert row.requested_module_name == "Diabetes Counseling Refresh"


async def test_invalid_module_no_op(patch_session_local, db_session: AsyncSession) -> None:
    chw = _test_chw_id()
    await training_request_event_worker.process_training_request_event_job(
        {
            "event_type": "module_requested",
            "event_id": str(uuid4()),
            "chw_id": chw,
            "module_id": str(uuid4()),
        }
    )
    rows = (await db_session.execute(select(CHWTrainingRequest))).scalars().all()
    assert rows == []


async def test_unpublished_module_no_op(patch_session_local, db_session: AsyncSession) -> None:
    chw = _test_chw_id()
    module = await _seed_published_module(db_session, lifecycle_status="draft")
    await training_request_event_worker.process_training_request_event_job(
        {
            "event_type": "module_requested",
            "event_id": str(uuid4()),
            "chw_id": chw,
            "module_id": str(module.id),
        }
    )
    rows = (await db_session.execute(select(CHWTrainingRequest))).scalars().all()
    assert rows == []


async def test_chatbot_faqs_only_no_op(patch_session_local, db_session: AsyncSession) -> None:
    chw = _test_chw_id()
    module = await _seed_published_module(db_session, chatbot_faqs_only=True)
    await training_request_event_worker.process_training_request_event_job(
        {
            "event_type": "module_requested",
            "event_id": str(uuid4()),
            "chw_id": chw,
            "module_id": str(module.id),
        }
    )
    rows = (await db_session.execute(select(CHWTrainingRequest))).scalars().all()
    assert rows == []


async def test_duplicate_module_no_op(patch_session_local, db_session: AsyncSession) -> None:
    chw = _test_chw_id()
    module = await _seed_published_module(db_session)
    payload = {
        "event_type": "module_requested",
        "chw_id": chw,
        "module_id": str(module.id),
    }
    await training_request_event_worker.process_training_request_event_job(
        {**payload, "event_id": str(uuid4())}
    )
    await training_request_event_worker.process_training_request_event_job(
        {**payload, "event_id": str(uuid4())}
    )
    rows = (
        (await db_session.execute(select(CHWTrainingRequest).where(CHWTrainingRequest.chw_id == chw)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_duplicate_custom_name_no_op(patch_session_local, db_session: AsyncSession) -> None:
    chw = _test_chw_id()
    await training_request_event_worker.process_training_request_event_job(
        {
            "event_type": "module_requested",
            "event_id": str(uuid4()),
            "chw_id": chw,
            "requested_module_name": "Diabetes Counseling Refresh",
        }
    )
    await training_request_event_worker.process_training_request_event_job(
        {
            "event_type": "module_requested",
            "event_id": str(uuid4()),
            "chw_id": chw,
            "requested_module_name": "  diabetes counseling refresh  ",
        }
    )
    rows = (
        (await db_session.execute(select(CHWTrainingRequest).where(CHWTrainingRequest.chw_id == chw)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_missing_identity_no_op(patch_session_local, db_session: AsyncSession) -> None:
    await training_request_event_worker.process_training_request_event_job(
        {
            "event_type": "module_requested",
            "event_id": str(uuid4()),
            "chw_id": _test_chw_id(),
            "reason": "No module selected",
        }
    )
    rows = (await db_session.execute(select(CHWTrainingRequest))).scalars().all()
    assert rows == []
