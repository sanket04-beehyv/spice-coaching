"""LLM classifier: associate a module with referral-domain behavioural_gap registry codes."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    ModelPolicy,
    PromptSpec,
    TraceContext,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.localized import deployment_locales, migrate_legacy_card
from platform_service.services.card_body_text import card_body_plain_text
from platform_service.services.llm_response_resolver import resolve_parsed_dict
from platform_service.services.prompts.gap_classification_prompt import (
    GAP_CLASSIFICATION_TEMPLATE_ID,
    GAP_CLASSIFICATION_TEMPLATE_VERSION,
    render_human_message,
    render_system_prompt,
)

logger = logging.getLogger(__name__)

_MODULE_PRIMARY_GAP_PREFIX = "module_primary_gap_"
_REFERRAL_DOMAIN = "referral"


@dataclass(frozen=True)
class GapClassificationResult:
    associated_gap_ids: list[UUID]
    associated_gap_codes: list[str]
    rationale: str


def _truncated_localized_field(
    localized: dict[str, str] | None,
    *,
    field: str,
    max_len: int,
) -> dict[str, str]:
    if not isinstance(localized, dict):
        return {}
    out: dict[str, str] = {}
    for locale, raw in localized.items():
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = card_body_plain_text(raw) if field == "body" else raw.strip()
        piece = _truncate(text, max_len)
        if piece:
            out[locale] = piece
    return out


def module_payload_for_classification(module: Module) -> dict[str, Any]:
    """Build a compact module summary for the classification prompt."""
    settings = get_settings()
    primary = deployment_locales(settings)
    cards = (module.module_json or {}).get("cards", [])
    card_summaries: list[dict[str, Any]] = []
    for idx, card in enumerate(cards, start=1):
        if not isinstance(card, dict):
            continue
        migrated = migrate_legacy_card(dict(card), primary=primary)
        summary: dict[str, Any] = {"card_index": idx}
        title = migrated.get("title")
        if isinstance(title, dict):
            truncated_title = _truncated_localized_field(title, field="title", max_len=400)
            if truncated_title:
                summary["title"] = truncated_title
        body = migrated.get("body")
        if isinstance(body, dict):
            truncated_body = _truncated_localized_field(body, field="body", max_len=400)
            if truncated_body:
                summary["body"] = truncated_body
        next_action = migrated.get("next_action")
        if isinstance(next_action, dict):
            truncated_action = _truncated_localized_field(next_action, field="next_action", max_len=200)
            if truncated_action:
                summary["next_action"] = truncated_action
        card_summaries.append(summary)
    return {
        "title": module.title_localized,
        "description": module.description_localized,
        "domain": module.domain,
        "sub_domain": module.sub_domain,
        "module_type": module.module_type,
        "cards": card_summaries,
    }


def _truncate(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


class ModuleGapClassifier:
    def __init__(
        self,
        session: AsyncSession,
        *,
        client: AIRuntimeClient | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self._session = session
        self._client = client or get_ai_client()
        self._model = model or settings.text_model
        self._max_associations = settings.gap_classification_max_associations

    async def load_registry_gaps(self) -> list[BehaviouralGap]:
        """Active referral-domain registry gaps (secondary classification candidates)."""
        result = await self._session.execute(
            select(BehaviouralGap)
            .where(BehaviouralGap.status == "active")
            .where(BehaviouralGap.domain == _REFERRAL_DOMAIN)
            .order_by(BehaviouralGap.gap_code.asc())
        )
        gaps = list(result.scalars().all())
        return [g for g in gaps if not g.gap_code.startswith(_MODULE_PRIMARY_GAP_PREFIX)]

    async def classify_module(self, module: Module) -> GapClassificationResult:
        registry = await self.load_registry_gaps()
        if not registry:
            return GapClassificationResult(
                associated_gap_ids=[],
                associated_gap_codes=[],
                rationale="No active referral-domain behavioural gaps in registry.",
            )

        registry_by_code = {g.gap_code: g for g in registry}
        registry_payload = [
            {
                "gap_code": g.gap_code,
                "description": g.description,
                "domain": g.domain,
            }
            for g in registry
        ]
        module_payload = module_payload_for_classification(module)

        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.MODULE_GAP_CLASSIFICATION,
            model_policy=ModelPolicy(model=self._model),
            prompt=PromptSpec(
                template_id=GAP_CLASSIFICATION_TEMPLATE_ID,
                template_version=GAP_CLASSIFICATION_TEMPLATE_VERSION,
                resolved_system_prompt=render_system_prompt(max_associations=self._max_associations),
                resolved_human_message=render_human_message(
                    module_payload=module_payload,
                    registry_gaps=registry_payload,
                ),
            ),
            constraints=GenerationConstraints(language="en", output_format="json"),
            trace_context=TraceContext(),
        )
        response = await self._client.generate(request)
        if response.error:
            logger.error(
                "Gap classifier: ai-runtime error for module %s: %s",
                module.id,
                response.error,
            )
            return GapClassificationResult(
                associated_gap_ids=[],
                associated_gap_codes=[],
                rationale="",
            )

        try:
            payload = resolve_parsed_dict(response)
        except json.JSONDecodeError as exc:
            logger.error(
                "Gap classifier: LLM output not JSON for module %s: %s",
                module.id,
                exc,
            )
            return GapClassificationResult(
                associated_gap_ids=[],
                associated_gap_codes=[],
                rationale="",
            )
        except TypeError:
            logger.error("Gap classifier: unexpected payload shape for module %s", module.id)
            return GapClassificationResult(
                associated_gap_ids=[],
                associated_gap_codes=[],
                rationale="",
            )

        raw_codes = payload.get("associated_gap_codes") or []
        rationale = str(payload.get("rationale") or "").strip()
        if not isinstance(raw_codes, list):
            raw_codes = []

        accepted_codes: list[str] = []
        accepted_ids: list[UUID] = []
        for raw in raw_codes:
            if not isinstance(raw, str):
                continue
            code = raw.strip()
            if not code or code.startswith(_MODULE_PRIMARY_GAP_PREFIX):
                continue
            gap = registry_by_code.get(code)
            if gap is None:
                logger.warning(
                    "Gap classifier: dropping unknown gap_code %r for module %s",
                    code,
                    module.id,
                )
                continue
            if gap.id in accepted_ids:
                continue
            accepted_codes.append(code)
            accepted_ids.append(gap.id)
            if len(accepted_ids) >= self._max_associations:
                break

        return GapClassificationResult(
            associated_gap_ids=accepted_ids,
            associated_gap_codes=accepted_codes,
            rationale=rationale,
        )
