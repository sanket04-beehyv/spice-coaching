"""Classify ingested modules against canonical assessment-due topic keys."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    ModelPolicy,
    PromptSpec,
    TraceContext,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.assessment_topic_catalog import (
    canonical_assessment_topic_keys,
    is_canonical_assessment_topic,
    normalize_topic_key,
    related_topic_keys,
)
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.module_gap_classifier import module_payload_for_classification
from platform_service.services.prompts.assessment_topic_classification_prompt import (
    ASSESSMENT_TOPIC_CLASSIFICATION_TEMPLATE_ID,
    ASSESSMENT_TOPIC_CLASSIFICATION_TEMPLATE_VERSION,
    render_human_message,
    render_system_prompt,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssessmentTopicClassificationResult:
    assessment_topics: list[str]
    primary_topic: str | None
    rationale: str
    source: str  # llm | metadata_rules | empty


def classify_topics_from_metadata(module: Module) -> AssessmentTopicClassificationResult:
    """Rule-based fallback using search_metadata topic_tags / clinical_conditions."""
    metadata = module.search_metadata_jsonb or {}
    raw_tags: set[str] = set()
    for field in ("topic_tags", "clinical_conditions"):
        values = metadata.get(field)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, str) and item.strip():
                raw_tags.add(normalize_topic_key(item))

    allowed = canonical_assessment_topic_keys()
    matched: list[str] = []
    for tag in raw_tags:
        expanded = related_topic_keys(tag)
        for key in expanded:
            if key in allowed and key not in matched:
                matched.append(key)

    if not matched:
        return AssessmentTopicClassificationResult(
            assessment_topics=[],
            primary_topic=None,
            rationale="No canonical assessment topics matched search metadata.",
            source="metadata_rules",
        )

    primary = matched[0]
    return AssessmentTopicClassificationResult(
        assessment_topics=matched,
        primary_topic=primary,
        rationale="Derived from search_metadata topic overlap with assessment topic catalog.",
        source="metadata_rules",
    )


class AssessmentTopicClassifier:
    def __init__(
        self,
        session: AsyncSession,
        *,
        client: AIRuntimeClient | None = None,
        model: str | None = None,
    ) -> None:
        del session  # reserved for future registry lookups
        settings = get_settings()
        self._client = client or get_ai_client()
        self._model = model or settings.text_model
        self._max_topics = settings.trigger_binding_max_topics
        self._allowed = sorted(canonical_assessment_topic_keys())

    async def classify_module(self, module: Module) -> AssessmentTopicClassificationResult:
        module_payload = module_payload_for_classification(module)
        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.MODULE_ASSESSMENT_TOPIC_CLASSIFICATION,
            model_policy=ModelPolicy(model=self._model),
            prompt=PromptSpec(
                template_id=ASSESSMENT_TOPIC_CLASSIFICATION_TEMPLATE_ID,
                template_version=ASSESSMENT_TOPIC_CLASSIFICATION_TEMPLATE_VERSION,
                resolved_system_prompt=render_system_prompt(
                    max_topics=self._max_topics,
                    allowed_topics=self._allowed,
                ),
                resolved_human_message=render_human_message(
                    module_payload=module_payload,
                    search_metadata=module.search_metadata_jsonb,
                    allowed_topics=self._allowed,
                ),
            ),
            constraints=GenerationConstraints(language="en", output_format="json"),
            trace_context=TraceContext(),
        )
        response = await self._client.generate(request)
        if response.error:
            logger.warning(
                "Assessment topic classifier: ai-runtime error for module %s: %s",
                module.id,
                response.error,
            )
            return classify_topics_from_metadata(module)

        try:
            payload = resolve_parsed_dict(response)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Assessment topic classifier: bad LLM payload for module %s", module.id)
            return classify_topics_from_metadata(module)

        return self._parse_llm_payload(payload, module)

    def _parse_llm_payload(
        self, payload: dict[str, Any], module: Module
    ) -> AssessmentTopicClassificationResult:
        raw_topics = payload.get("assessment_topics") or []
        rationale = str(payload.get("rationale") or "").strip()
        primary_raw = payload.get("primary_topic")
        if not isinstance(raw_topics, list):
            raw_topics = []

        accepted: list[str] = []
        for raw in raw_topics:
            if not isinstance(raw, str):
                continue
            key = normalize_topic_key(raw)
            if not is_canonical_assessment_topic(key):
                logger.warning(
                    "Assessment topic classifier: dropping unknown topic %r for module %s",
                    raw,
                    module.id,
                )
                continue
            if key not in accepted:
                accepted.append(key)
            if len(accepted) >= self._max_topics:
                break

        if not accepted:
            fallback = classify_topics_from_metadata(module)
            if fallback.assessment_topics:
                return fallback
            return AssessmentTopicClassificationResult(
                assessment_topics=[],
                primary_topic=None,
                rationale=rationale or "No valid assessment topics returned.",
                source="llm",
            )

        primary: str | None = None
        if isinstance(primary_raw, str):
            candidate = normalize_topic_key(primary_raw)
            if candidate in accepted:
                primary = candidate
        if primary is None:
            primary = accepted[0]

        return AssessmentTopicClassificationResult(
            assessment_topics=accepted,
            primary_topic=primary,
            rationale=rationale,
            source="llm",
        )
