"""LLM synthesis of chat FAQs from semantic question clusters."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    TraceContext,
)
from mc_contracts.localized import LocalizedString

from platform_service.config import Settings, get_settings
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.localized import migrate_legacy_suffix_field, primary_text, to_localized_string
from platform_service.services.chat_faq_aggregator import stable_faq_id
from platform_service.services.chat_faq_clusterer import QuestionCluster
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.prompt_registry import CHAT_FAQ_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.chat_faq_variables import build_chat_faq_variables
from platform_service.services.question_text import normalize_question

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SynthesizedChatFaq:
    id: UUID
    question_localized: LocalizedString
    normalized_question: str
    occurrence_count: int
    rank: int
    last_seen_at: datetime | None


def _clusters_payload(clusters: list[QuestionCluster]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for index, cluster in enumerate(clusters):
        payload.append(
            {
                "cluster_index": index,
                "total_count": cluster.total_count,
                "members": [
                    {
                        "text": member.text,
                        "occurrence_count": member.occurrence_count,
                    }
                    for member in cluster.members
                ],
            }
        )
    return payload


def _parse_faq_items(
    payload: dict[str, Any],
    *,
    max_items: int,
    primary_locale: str,
) -> list[dict[str, Any]]:
    raw = payload.get("faqs")
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question_map = item.get("question")
        if isinstance(question_map, dict):
            question_primary = str(question_map.get(primary_locale) or "").strip()
        else:
            migrated = dict(item)
            migrate_legacy_suffix_field(migrated, "question", primary=primary_locale)
            question_map = migrated.get("question")
            question_primary = (primary_text(question_map) if isinstance(question_map, dict) else "") or ""
        if not question_primary:
            continue
        cluster_index = item.get("source_cluster_index")
        if not isinstance(cluster_index, int):
            continue
        items.append(
            {
                "question_primary": question_primary,
                "source_cluster_index": cluster_index,
            }
        )
        if len(items) >= max_items:
            break
    return items


def _fallback_faqs(
    *,
    tenant_id: UUID,
    clusters: list[QuestionCluster],
) -> list[SynthesizedChatFaq]:
    results: list[SynthesizedChatFaq] = []
    for rank, cluster in enumerate(clusters, start=1):
        seed = cluster.seed_text.strip()
        if not seed:
            continue
        normalized = normalize_question(seed)
        results.append(
            SynthesizedChatFaq(
                id=stable_faq_id(tenant_id=tenant_id, normalized_question_en=normalized),
                question_localized=to_localized_string(seed),
                normalized_question=normalized,
                occurrence_count=cluster.total_count,
                rank=rank,
                last_seen_at=cluster.last_seen_at,
            )
        )
    return results


class ChatFaqGenerator:
    def __init__(
        self,
        *,
        client: AIRuntimeClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = client or get_ai_client()
        self._target_count = self._settings.chat_faq_target_count

    async def synthesize(
        self,
        tenant_id: UUID,
        clusters: list[QuestionCluster],
    ) -> list[SynthesizedChatFaq]:
        if not clusters:
            return []

        selected = clusters[: self._target_count]
        rendered = await PromptTemplateService().render(
            None,
            template_id=CHAT_FAQ_TEMPLATE_ID,
            variant_key=None,
            variables=build_chat_faq_variables(
                target_count=len(selected),
                clusters_payload=_clusters_payload(selected),
                deployment_primary_locale=self._settings.deployment_primary_locale,
                deployment_region_context=self._settings.deployment_region_context,
            ),
        )
        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.CHAT_FAQ_SYNTHESIS,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(language="en", output_format="json"),
            trace_context=TraceContext(),
        )

        try:
            response = await self._client.generate(request)
        except Exception:
            logger.exception("Chat FAQ generator: ai-runtime call failed for tenant %s", tenant_id)
            return _fallback_faqs(tenant_id=tenant_id, clusters=selected)

        if response.error:
            logger.warning(
                "Chat FAQ generator: ai-runtime error for tenant %s: %s",
                tenant_id,
                response.error,
            )
            return _fallback_faqs(tenant_id=tenant_id, clusters=selected)

        try:
            resolved = resolve_parsed_dict(response)
            items = _parse_faq_items(
                resolved,
                max_items=len(selected),
                primary_locale=self._settings.deployment_primary_locale,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning(
                "Chat FAQ generator: invalid LLM payload for tenant %s: %s",
                tenant_id,
                exc,
            )
            return _fallback_faqs(tenant_id=tenant_id, clusters=selected)

        if not items:
            return _fallback_faqs(tenant_id=tenant_id, clusters=selected)

        results: list[SynthesizedChatFaq] = []
        for rank, item in enumerate(items, start=1):
            cluster_index = item["source_cluster_index"]
            if cluster_index < 0 or cluster_index >= len(selected):
                continue
            cluster = selected[cluster_index]
            question_primary = item["question_primary"]
            normalized = normalize_question(question_primary)
            results.append(
                SynthesizedChatFaq(
                    id=stable_faq_id(tenant_id=tenant_id, normalized_question_en=normalized),
                    question_localized=to_localized_string(question_primary),
                    normalized_question=normalized,
                    occurrence_count=cluster.total_count,
                    rank=rank,
                    last_seen_at=cluster.last_seen_at,
                )
            )
        return results if results else _fallback_faqs(tenant_id=tenant_id, clusters=selected)
