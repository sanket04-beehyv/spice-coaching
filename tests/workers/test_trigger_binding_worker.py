"""Trigger binding worker tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.trigger_repository import TriggerRepository
from platform_service.services.assessment_topic_classifier import AssessmentTopicClassificationResult
from platform_service.workers.trigger_binding_worker import bind_assessment_triggers_for_module

from tests.conftest import requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_bind_triggers_writes_bindings(db_session) -> None:
    family = ModuleFamily(module_code=f"fam-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": "ANC visit prep"},
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="draft",
        module_json={"cards": [{"title": {"bn": "c"}, "body": {"bn": "b"}}]},
        search_metadata_jsonb={"topic_tags": ["anc"], "clinical_conditions": []},
    )
    db_session.add(module)
    await db_session.flush()

    repo = TriggerRepository(db_session)
    trigger = await repo.create_trigger(
        trigger_kind="workflow_event",
        trigger_code="wf:assessment_due:anc",
        predicate_jsonb={
            "spice_event_code": "assessment_due",
            "filter_predicate": {"assessment_topic": "anc", "match": {}},
        },
    )
    await db_session.commit()

    classification = AssessmentTopicClassificationResult(
        assessment_topics=["anc"],
        primary_topic="anc",
        rationale="test",
        source="metadata_rules",
    )
    with patch("platform_service.workers.trigger_binding_worker.AssessmentTopicClassifier") as mock_cls:
        mock_cls.return_value.classify_module = AsyncMock(return_value=classification)
        count = await bind_assessment_triggers_for_module(module.id)

    assert count == 1
    bindings = await repo.list_bindings_with_triggers_for_module(module.id)
    assert len(bindings) == 1
    assert bindings[0][1].id == trigger.id
    assert bindings[0][0].relationship == "primary"


async def test_bind_triggers_skips_when_no_topics(db_session) -> None:
    family = ModuleFamily(module_code=f"fam-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": "Generic"},
        domain="hypertension",
        module_type="refresher",
        lifecycle_status="draft",
        module_json={"cards": [{"title": {"bn": "c"}, "body": {"bn": "b"}}]},
    )
    db_session.add(module)
    await db_session.commit()

    classification = AssessmentTopicClassificationResult(
        assessment_topics=[],
        primary_topic=None,
        rationale="none",
        source="llm",
    )
    with patch("platform_service.workers.trigger_binding_worker.AssessmentTopicClassifier") as mock_cls:
        mock_cls.return_value.classify_module = AsyncMock(return_value=classification)
        count = await bind_assessment_triggers_for_module(module.id)

    assert count == 0
    repo = TriggerRepository(db_session)
    bindings = await repo.list_bindings_with_triggers_for_module(module.id)
    assert bindings == []
