"""Triggers sync bundle includes assessment-due bindings after ingest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.trigger_repository import TriggerRepository
from platform_service.services.assessment_patient_match import assessment_due_predicate
from platform_service.services.sync_service import SyncService
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_triggers_bundle_includes_assessment_due_binding(db_session: AsyncSession) -> None:
    family = ModuleFamily(module_code=f"sync-trig-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()

    repo = TriggerRepository(db_session)
    trigger = await repo.create_trigger(
        trigger_kind="workflow_event",
        trigger_code="wf:assessment_due:malaria",
        predicate_jsonb=assessment_due_predicate("malaria"),
    )

    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": "Malaria module"},
        domain="iccm",
        module_type="refresher",
        lifecycle_status="published",
        module_json={"cards": []},
    )
    db_session.add(module)
    await db_session.flush()

    await repo.bind_module_to_trigger(
        module_id=module.id,
        trigger_definition_id=trigger.id,
        relationship="primary",
        priority_weight=20,
    )
    await db_session.commit()

    since = datetime.now(UTC) - timedelta(hours=1)
    bundle = await SyncService(db_session).get_triggers_bundle(since=since)
    trigger_ids = {item.id for item in bundle.triggers}
    assert trigger.id in trigger_ids
    family_bindings = [b for b in bundle.bindings if b.module_id == module.id]
    assert len(family_bindings) == 1
    assert family_bindings[0].trigger_definition_id == trigger.id
