"""Unit tests for unattributed demand aggregation and suggestion classification."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, PromptSpec, TokenUsage
from platform_service.services.module_creation_suggestion_classifier import (
    SUGGESTION_KIND_MATCHED_DRAFT,
    SUGGESTION_KIND_PROPOSED_TOPIC,
    DraftCatalogItem,
    ModuleCreationSuggestionClassifier,
)
from platform_service.services.unattributed_demand_aggregator import (
    DedupedEvidence,
    UnattributedDemandAggregator,
)


def _inference_response(
    *,
    parsed_json: dict | None = None,
    raw_text: str = "",
    error: str | None = None,
) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-mcs",
        generation_type=GenerationType.MODULE_CREATION_SUGGESTION,
        provider="google",
        model="gemini-2.5-flash",
        max_tokens=8192,
        temperature=0.2,
        raw_text=raw_text,
        parsed_json=parsed_json,
        latency_ms=10,
        token_usage=TokenUsage(input=10, output=10),
        error=error,
    )


_PROMPT_SPEC = PromptSpec(
    template_id="module_creation_suggestion",
    template_version=1,
    resolved_system_prompt="system",
    resolved_human_message="human",
)


def test_dedupe_merges_normalized_questions_and_requests() -> None:
    agg = UnattributedDemandAggregator(MagicMock())
    rows = [
        {
            "id": "e1",
            "source": "digital_help",
            "text": "  How to refer? ",
            "normalized_text": "how to refer?",
            "timestamp_utc": datetime(2026, 7, 29, 10, 0, tzinfo=UTC),
            "chw_id": 11,
        },
        {
            "id": "e2",
            "source": "digital_help",
            "text": "how to refer?",
            "normalized_text": "how to refer?",
            "timestamp_utc": datetime(2026, 7, 29, 12, 0, tzinfo=UTC),
            "chw_id": 12,
        },
        {
            "id": "e3",
            "source": "module_requested",
            "text": "Neonatal Care",
            "normalized_text": "neonatal care",
            "timestamp_utc": datetime(2026, 7, 29, 9, 0, tzinfo=UTC),
            "chw_id": 13,
        },
        {
            "id": "e4",
            "source": "digital_help",
            "text": "   ",
            "normalized_text": "",
            "timestamp_utc": datetime(2026, 7, 29, 8, 0, tzinfo=UTC),
            "chw_id": 14,
        },
    ]
    questions, requests = agg._dedupe(rows)
    assert len(questions) == 1
    assert questions[0].occurrence_count == 2
    assert questions[0].text == "how to refer?"
    assert questions[0].sample_chw_id == 12
    assert len(requests) == 1
    assert requests[0].normalized_text == "neonatal care"


@pytest.mark.asyncio
async def test_classifier_keeps_valid_draft_and_proposed_drops_unknown() -> None:
    draft_id = uuid4()
    session = MagicMock()
    client = MagicMock()
    client.generate = AsyncMock(
        return_value=_inference_response(
            parsed_json={
                "suggestions": [
                    {
                        "matched_module_id": str(draft_id),
                        "proposed_topic": None,
                        "rationale": "Matches draft",
                        "evidence_question_keys": ["bp check"],
                        "evidence_request_keys": [],
                    },
                    {
                        "matched_module_id": str(uuid4()),
                        "proposed_topic": None,
                        "rationale": "Unknown draft",
                        "evidence_question_keys": ["bp check"],
                        "evidence_request_keys": [],
                    },
                    {
                        "matched_module_id": None,
                        "proposed_topic": "Postpartum hemorrhage",
                        "rationale": "New topic",
                        "evidence_question_keys": [],
                        "evidence_request_keys": ["pph care"],
                    },
                    {
                        "matched_module_id": None,
                        "proposed_topic": "Orphan",
                        "rationale": "No evidence keys",
                        "evidence_question_keys": ["missing"],
                        "evidence_request_keys": [],
                    },
                ]
            }
        )
    )
    settings = MagicMock()
    settings.module_creation_suggestions_max_suggestions = 20
    settings.module_creation_suggestions_max_evidence = 80

    rendered = MagicMock()
    with (
        patch(
            "platform_service.services.module_creation_suggestion_classifier.PromptTemplateService.render",
            new_callable=AsyncMock,
            return_value=rendered,
        ),
        patch(
            "platform_service.services.module_creation_suggestion_classifier.prompt_spec_from_rendered",
            return_value=_PROMPT_SPEC,
        ),
    ):
        classifier = ModuleCreationSuggestionClassifier(session, client=client, settings=settings)
        result = await classifier.classify(
            suggestion_date=date(2026, 7, 29),
            drafts=[DraftCatalogItem(module_id=draft_id, title="BP Screening")],
            questions=[
                DedupedEvidence(
                    source="digital_help",
                    text="BP check",
                    normalized_text="bp check",
                    occurrence_count=3,
                    last_seen_at=None,
                    sample_event_id="e1",
                    sample_chw_id=1,
                )
            ],
            requests=[
                DedupedEvidence(
                    source="module_requested",
                    text="PPH care",
                    normalized_text="pph care",
                    occurrence_count=2,
                    last_seen_at=None,
                    sample_event_id="e2",
                    sample_chw_id=2,
                )
            ],
        )

    assert len(result) == 2
    assert result[0].suggestion_kind == SUGGESTION_KIND_MATCHED_DRAFT
    assert result[0].matched_module_id == draft_id
    assert result[1].suggestion_kind == SUGGESTION_KIND_PROPOSED_TOPIC
    assert result[1].proposed_topic == "Postpartum hemorrhage"


@pytest.mark.asyncio
async def test_classifier_raises_on_invalid_json() -> None:
    session = MagicMock()
    client = MagicMock()
    client.generate = AsyncMock(return_value=_inference_response(raw_text="not-json", parsed_json=None))
    settings = MagicMock()
    settings.module_creation_suggestions_max_suggestions = 20
    settings.module_creation_suggestions_max_evidence = 80
    rendered = MagicMock()
    with (
        patch(
            "platform_service.services.module_creation_suggestion_classifier.PromptTemplateService.render",
            new_callable=AsyncMock,
            return_value=rendered,
        ),
        patch(
            "platform_service.services.module_creation_suggestion_classifier.prompt_spec_from_rendered",
            return_value=_PROMPT_SPEC,
        ),
    ):
        classifier = ModuleCreationSuggestionClassifier(session, client=client, settings=settings)
        with pytest.raises(RuntimeError, match="invalid LLM JSON"):
            await classifier.classify(
                suggestion_date=date(2026, 7, 29),
                drafts=[],
                questions=[],
                requests=[],
            )


@pytest.mark.asyncio
async def test_classifier_raises_on_ai_runtime_error() -> None:
    session = MagicMock()
    client = MagicMock()
    client.generate = AsyncMock(return_value=_inference_response(error="provider down"))
    settings = MagicMock()
    settings.module_creation_suggestions_max_suggestions = 20
    settings.module_creation_suggestions_max_evidence = 80
    with (
        patch(
            "platform_service.services.module_creation_suggestion_classifier.PromptTemplateService.render",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
        patch(
            "platform_service.services.module_creation_suggestion_classifier.prompt_spec_from_rendered",
            return_value=_PROMPT_SPEC,
        ),
    ):
        classifier = ModuleCreationSuggestionClassifier(session, client=client, settings=settings)
        with pytest.raises(RuntimeError, match="ai-runtime error"):
            await classifier.classify(
                suggestion_date=date(2026, 7, 29),
                drafts=[],
                questions=[],
                requests=[],
            )
