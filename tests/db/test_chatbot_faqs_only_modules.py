"""Chatbot-FAQ-only module exclusion from CHW training workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from mc_contracts.admin_assignments import AssignmentCreateRequest
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.module_family_repository import ModuleFamilyRepository
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.db.repositories.trigger_repository import TriggerRepository
from platform_service.services.module_assignment_service import (
    AssignmentValidationError,
    ModuleAssignmentService,
)
from platform_service.services.module_selector import ModuleSelector
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db


async def _seed_published_module(
    session: AsyncSession,
    *,
    chatbot_faqs_only: bool = False,
) -> tuple[ModuleFamily, Module]:
    family = ModuleFamily(
        module_code=f"faq-{uuid4().hex[:8]}",
    )
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": "test"},
        domain="clinical",
        module_type="refresher",
        lifecycle_status="published",
        module_json={"cards": [{"title": {"bn": "c"}, "body": {"bn": "b"}}]},
        published_at=datetime.now(UTC),
        chatbot_faqs_only=chatbot_faqs_only,
    )
    session.add(module)
    await session.flush()
    family.current_published_module_id = module.id
    await session.commit()
    return family, module


@pytest.mark.asyncio
@requires_db
async def test_family_not_assignable_when_chatbot_faqs_only(db_session: AsyncSession) -> None:
    family, _module = await _seed_published_module(db_session, chatbot_faqs_only=True)
    repo = ModuleFamilyRepository(db_session)
    assert await repo.is_assignable(family.id) is False


@pytest.mark.asyncio
@requires_db
async def test_get_published_module_for_family_returns_none_for_faq_only(
    db_session: AsyncSession,
) -> None:
    family, _module = await _seed_published_module(db_session, chatbot_faqs_only=True)
    repo = ModuleRepository(db_session)
    assert await repo.get_published_module_for_family(family.id) is None


@pytest.mark.asyncio
@requires_db
async def test_assignment_service_rejects_chatbot_faq_module(db_session: AsyncSession) -> None:
    _family, module = await _seed_published_module(db_session, chatbot_faqs_only=True)
    service = ModuleAssignmentService(db_session)
    with pytest.raises(AssignmentValidationError):
        await service.create_assignments(
            AssignmentCreateRequest(
                module_id=module.id,
                assignment_type="individual",
                user_ids=[1313053891],
            ),
            assigned_by=1,
        )


@pytest.mark.asyncio
@requires_db
async def test_module_selector_skips_chatbot_faq_bindings(db_session: AsyncSession) -> None:
    family, module = await _seed_published_module(db_session, chatbot_faqs_only=True)
    trigger_repo = TriggerRepository(db_session)
    trigger = await trigger_repo.create_trigger(
        trigger_kind="gap",
        trigger_code=f"trig_{uuid4().hex[:8]}",
        predicate_jsonb={"behavioural_gap_code": "x"},
    )
    await trigger_repo.bind_module_to_trigger(
        module_id=module.id,
        trigger_definition_id=trigger.id,
        priority_weight=10,
    )
    await db_session.commit()

    selector = ModuleSelector(db_session)
    out = await selector.select_modules_for_chw(
        chw_id=uuid4().int % (10**15) + 1,
        fired_trigger_codes=[trigger.trigger_code],
    )
    assert out == []
