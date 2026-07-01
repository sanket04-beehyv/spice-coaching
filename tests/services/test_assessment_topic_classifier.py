"""Assessment topic classifier tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, TokenUsage
from platform_service.db.models.module import Module
from platform_service.services.assessment_topic_classifier import (
    AssessmentTopicClassifier,
    classify_topics_from_metadata,
)


def _module_with_metadata() -> Module:
    return Module(
        module_family_id=uuid4(),
        version=1,
        title_localized={"bn": "Malaria follow-up"},
        domain="iccm",
        module_type="refresher",
        lifecycle_status="draft",
        module_json={"cards": [{"title": {"bn": "t"}, "body": {"bn": "malaria treatment"}}]},
        search_metadata_jsonb={"topic_tags": ["malaria"], "clinical_conditions": []},
    )


def test_classify_topics_from_metadata_matches_topic_tags() -> None:
    result = classify_topics_from_metadata(_module_with_metadata())
    assert "malaria" in result.assessment_topics
    assert result.source == "metadata_rules"


@pytest.mark.asyncio
async def test_classify_module_falls_back_on_llm_error() -> None:
    module = _module_with_metadata()
    client = MagicMock()
    client.generate = AsyncMock(
        return_value=InferenceResponse(
            request_id="r1",
            generation_type=GenerationType.MODULE_ASSESSMENT_TOPIC_CLASSIFICATION,
            provider="openai",
            model="gpt-test",
            raw_text="",
            text="",
            error="provider down",
            latency_ms=1,
            usage=TokenUsage(input=0, output=0),
        )
    )
    classifier = AssessmentTopicClassifier(MagicMock(), client=client)
    result = await classifier.classify_module(module)
    assert "malaria" in result.assessment_topics
    assert result.source == "metadata_rules"
