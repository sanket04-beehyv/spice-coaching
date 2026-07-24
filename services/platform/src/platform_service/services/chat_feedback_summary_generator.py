"""LLM synthesis of weekly chat feedback summaries from telemetry events."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from mc_contracts.chat_feedback_summary import (
    ChatFeedbackEventCounts,
    ChatFeedbackEventSample,
    ChatFeedbackSummaryResponse,
)
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    TraceContext,
)

from platform_service.config import Settings, get_settings
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.chat_feedback_aggregator import FeedbackEvent, TenantFeedbackBatch
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.prompt_registry import CHAT_FEEDBACK_SUMMARY_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.chat_feedback_summary_variables import (
    build_chat_feedback_summary_variables,
)
from platform_service.services.prompts.chat_feedback_summary_prompt import fallback_summary

logger = logging.getLogger(__name__)


def _event_to_sample(event: FeedbackEvent) -> ChatFeedbackEventSample:
    event_type: str = event.event_type
    if event_type not in ("chat_feedback_positive", "chat_feedback_negative"):
        event_type = "chat_feedback_negative"
    return ChatFeedbackEventSample(
        event_id=event.event_id,
        event_type=event_type,  # type: ignore[arg-type]
        inference_mode=event.inference_mode,
        question=event.question,
        feedback=event.feedback or None,
        answer_excerpt=event.answer_excerpt,
        module_id=event.module_id,
        occurred_at=event.occurred_at,
    )


def _event_payload(event: FeedbackEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "inference_mode": event.inference_mode,
        "question": event.question,
        "feedback": event.feedback or None,
        "answer_excerpt": event.answer_excerpt,
        "module_id": str(event.module_id) if event.module_id is not None else None,
        "occurred_at": event.occurred_at.isoformat(),
    }


def _parse_llm_payload(payload: dict[str, Any]) -> dict[str, Any]:
    llm_summary = str(payload.get("llm_summary") or "").strip()
    positive_online = payload.get("positive_online_themes")
    positive_offline = payload.get("positive_offline_themes")
    negative_online = payload.get("negative_online_recommendations")
    negative_offline = payload.get("negative_offline_recommendations")

    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        items: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                items.append(text)
        return items[:8]

    return {
        "llm_summary": llm_summary,
        "positive_online_themes": _string_list(positive_online),
        "positive_offline_themes": _string_list(positive_offline),
        "negative_online_recommendations": _string_list(negative_online),
        "negative_offline_recommendations": _string_list(negative_offline),
    }


class ChatFeedbackSummaryGenerator:
    def __init__(
        self,
        *,
        client: AIRuntimeClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or get_ai_client()

    async def synthesize(
        self,
        *,
        batch: TenantFeedbackBatch,
        period_start: datetime | None,
        period_end: datetime,
        previous_summary: ChatFeedbackSummaryResponse | None = None,
        llm_timeout_seconds: float | None = None,
    ) -> ChatFeedbackSummaryResponse:
        counts = batch.event_counts()
        sampled_events = batch.sample_for_llm(
            max_positive_online=self._settings.chat_feedback_summary_max_positive_online_samples,
            max_positive_offline=self._settings.chat_feedback_summary_max_positive_offline_samples,
            max_negative_online=self._settings.chat_feedback_summary_max_negative_online_samples,
            max_negative_offline=self._settings.chat_feedback_summary_max_negative_offline_samples,
        )

        positive_online_payload = [
            _event_payload(event) for event in sampled_events if event.is_positive_online
        ]
        positive_offline_payload = [
            _event_payload(event) for event in sampled_events if event.is_positive_offline
        ]
        negative_online_payload = [
            _event_payload(event) for event in sampled_events if event.is_negative_online
        ]
        negative_offline_payload = [
            _event_payload(event) for event in sampled_events if event.is_negative_offline
        ]

        fallback = fallback_summary(
            event_counts=counts,
            positive_online_events=positive_online_payload,
            positive_offline_events=positive_offline_payload,
            negative_online_events=negative_online_payload,
            negative_offline_events=negative_offline_payload,
        )

        previous_payload: dict[str, Any] | None = None
        if previous_summary is not None:
            previous_payload = {
                "llm_summary": previous_summary.llm_summary,
                "positive_online_themes": previous_summary.positive_online_themes,
                "positive_offline_themes": previous_summary.positive_offline_themes,
                "negative_online_recommendations": previous_summary.negative_online_recommendations,
                "negative_offline_recommendations": previous_summary.negative_offline_recommendations,
            }

        llm_input = {
            "previous_summary": previous_payload,
            "event_counts": counts,
            "positive_online_events": positive_online_payload,
            "positive_offline_events": positive_offline_payload,
            "negative_online_events": negative_online_payload,
            "negative_offline_events": negative_offline_payload,
        }

        timeout_seconds = (
            llm_timeout_seconds
            if llm_timeout_seconds is not None
            else self._settings.chat_feedback_summary_llm_timeout_seconds
        )

        rendered = await PromptTemplateService().render(
            self._session,
            template_id=CHAT_FEEDBACK_SUMMARY_TEMPLATE_ID,
            variant_key=None,
            variables=build_chat_feedback_summary_variables(payload=llm_input),
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.CHAT_FEEDBACK_SUMMARY,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(language="en", output_format="json"),
            trace_context=TraceContext(),
        )

        parsed = fallback
        try:
            response = await asyncio.wait_for(
                self._client.generate(request),
                timeout=timeout_seconds,
            )
            if response.error:
                logger.warning(
                    "Chat feedback summary generator: ai-runtime error for tenant %s: %s",
                    batch.tenant_id,
                    response.error,
                )
            else:
                try:
                    resolved = resolve_parsed_dict(response)
                    candidate = _parse_llm_payload(resolved)
                    if candidate["llm_summary"]:
                        parsed = candidate
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    logger.warning(
                        "Chat feedback summary generator: invalid LLM payload for tenant %s: %s",
                        batch.tenant_id,
                        exc,
                    )
        except TimeoutError:
            logger.warning(
                "Chat feedback summary generator: ai-runtime timed out after %.1fs for tenant %s",
                timeout_seconds,
                batch.tenant_id,
            )
        except Exception:
            logger.exception(
                "Chat feedback summary generator: ai-runtime call failed for tenant %s",
                batch.tenant_id,
            )

        return ChatFeedbackSummaryResponse(
            generated_at=datetime.now(UTC),
            period_start=period_start,
            period_end=period_end,
            event_counts=ChatFeedbackEventCounts(**counts),
            llm_summary=parsed["llm_summary"],
            positive_online_themes=parsed["positive_online_themes"],
            positive_offline_themes=parsed["positive_offline_themes"],
            negative_online_recommendations=parsed["negative_online_recommendations"],
            negative_offline_recommendations=parsed["negative_offline_recommendations"],
            sampled_events=[_event_to_sample(event) for event in sampled_events],
        )
