"""LLM classifier: map unattributed demand to draft modules or new topics."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    TraceContext,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.prompt_registry import MODULE_CREATION_SUGGESTION_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.module_creation_suggestion_variables import (
    build_module_creation_suggestion_variables,
)
from platform_service.services.unattributed_demand_aggregator import DedupedEvidence

logger = logging.getLogger(__name__)

SUGGESTION_KIND_MATCHED_DRAFT = "matched_draft"
SUGGESTION_KIND_PROPOSED_TOPIC = "proposed_topic"


@dataclass(frozen=True)
class DraftCatalogItem:
    module_id: UUID
    title: str


@dataclass(frozen=True)
class ClassifiedSuggestion:
    suggestion_kind: str
    matched_module_id: UUID | None
    proposed_topic: str | None
    display_title: str
    rationale: str | None
    question_keys: list[str]
    request_keys: list[str]


class ModuleCreationSuggestionClassifier:
    def __init__(
        self,
        session: AsyncSession,
        *,
        client: AIRuntimeClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._client = client or get_ai_client()
        self._settings = settings or get_settings()

    async def classify(
        self,
        *,
        suggestion_date: date,
        drafts: list[DraftCatalogItem],
        questions: list[DedupedEvidence],
        requests: list[DedupedEvidence],
    ) -> list[ClassifiedSuggestion]:
        max_suggestions = max(1, int(self._settings.module_creation_suggestions_max_suggestions))
        max_evidence = max(1, int(self._settings.module_creation_suggestions_max_evidence))

        capped_questions = questions[:max_evidence]
        remaining = max(0, max_evidence - len(capped_questions))
        capped_requests = requests[:remaining] if remaining else []

        draft_by_id = {d.module_id: d for d in drafts}
        question_by_key = {q.normalized_text: q for q in capped_questions}
        request_by_key = {r.normalized_text: r for r in capped_requests}

        rendered = await PromptTemplateService().render(
            self._session,
            template_id=MODULE_CREATION_SUGGESTION_TEMPLATE_ID,
            variant_key=None,
            variables=build_module_creation_suggestion_variables(
                max_suggestions=max_suggestions,
                suggestion_date=suggestion_date.isoformat(),
                draft_catalog=[{"module_id": str(d.module_id), "title": d.title} for d in drafts],
                questions=[
                    {
                        "key": q.normalized_text,
                        "text": q.text,
                        "occurrence_count": q.occurrence_count,
                    }
                    for q in capped_questions
                ],
                requests=[
                    {
                        "key": r.normalized_text,
                        "text": r.text,
                        "occurrence_count": r.occurrence_count,
                    }
                    for r in capped_requests
                ],
            ),
        )

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.MODULE_CREATION_SUGGESTION,
            prompt=prompt_spec_from_rendered(rendered),
            constraints=GenerationConstraints(language="en", output_format="json"),
            trace_context=TraceContext(),
        )
        response = await self._client.generate(request)
        if response.error:
            raise RuntimeError(f"ai-runtime error: {response.error}")

        try:
            payload = resolve_parsed_dict(response)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid LLM JSON for module creation suggestions: {exc}") from exc

        raw_suggestions = payload.get("suggestions") if isinstance(payload, dict) else None
        if not isinstance(raw_suggestions, list):
            raise RuntimeError("LLM payload missing suggestions list")

        accepted: list[ClassifiedSuggestion] = []
        for raw in raw_suggestions:
            if not isinstance(raw, dict):
                continue
            parsed = self._parse_one(
                raw,
                draft_by_id=draft_by_id,
                question_by_key=question_by_key,
                request_by_key=request_by_key,
            )
            if parsed is None:
                continue
            accepted.append(parsed)
            if len(accepted) >= max_suggestions:
                break
        return accepted

    def _parse_one(
        self,
        raw: dict[str, Any],
        *,
        draft_by_id: dict[UUID, DraftCatalogItem],
        question_by_key: dict[str, DedupedEvidence],
        request_by_key: dict[str, DedupedEvidence],
    ) -> ClassifiedSuggestion | None:
        rationale = str(raw.get("rationale") or "").strip() or None
        q_keys = _string_list(raw.get("evidence_question_keys"))
        r_keys = _string_list(raw.get("evidence_request_keys"))
        q_keys = [k for k in q_keys if k in question_by_key]
        r_keys = [k for k in r_keys if k in request_by_key]
        if not q_keys and not r_keys:
            return None

        matched_raw = raw.get("matched_module_id")
        proposed = str(raw.get("proposed_topic") or "").strip() or None
        matched_id: UUID | None = None
        if matched_raw is not None and str(matched_raw).strip():
            try:
                matched_id = UUID(str(matched_raw).strip())
            except (TypeError, ValueError):
                logger.warning("Dropping suggestion with invalid matched_module_id=%r", matched_raw)
                matched_id = None

        if matched_id is not None:
            draft = draft_by_id.get(matched_id)
            if draft is None:
                logger.warning(
                    "Dropping suggestion with unknown draft module_id=%s",
                    matched_id,
                )
                return None
            return ClassifiedSuggestion(
                suggestion_kind=SUGGESTION_KIND_MATCHED_DRAFT,
                matched_module_id=matched_id,
                proposed_topic=None,
                display_title=draft.title,
                rationale=rationale,
                question_keys=q_keys,
                request_keys=r_keys,
            )

        if proposed:
            return ClassifiedSuggestion(
                suggestion_kind=SUGGESTION_KIND_PROPOSED_TOPIC,
                matched_module_id=None,
                proposed_topic=proposed,
                display_title=proposed,
                rationale=rationale,
                question_keys=q_keys,
                request_keys=r_keys,
            )
        return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out
