"""Post-publish quiz generation worker.

Per `docs/ARCHITECTURE_RESET.md`. Triggered on module publish (Stage 3
enqueues a Celery task per `module_id`). Reads the module's cards from
`module.module_json`, calls ai-runtime to generate scenario-based quiz
questions, and writes `module_quiz_question` rows linked to the module via
the `module_id` FK introduced in migration 0007.

Failure does not block the module — quiz generation can be retried later
or the questions can be authored manually from the admin dashboard. The
caller (Celery task entrypoint in `celery_tasks.py`) catches exceptions and
logs them; the module remains published with zero quiz questions until a
later run succeeds.

The prompt structure is intentionally close to knowledge-layer's
`app/services/quiz_generation.py` shape — simple, single LLM call, JSON
output validated against a Pydantic-style schema. The architecture reset
removed the multi-call distractor critique loop because empirically the
first-pass output is good enough for pilot scope; reviewer corrections from
the dashboard handle the long-tail.
"""

from __future__ import annotations

import json
import logging
import uuid
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
from mc_foundation.locale import localized_primary_text
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.deps import get_ai_client
from platform_service.localized import (
    deployment_locales,
    extract_localized_options_from_raw,
    extract_localized_string_from_raw,
    migrate_legacy_card,
)
from platform_service.services.card_body_text import card_body_plain_text
from platform_service.services.llm_response_resolver import resolve_parsed_json
from platform_service.services.post_publish_step import finish_post_publish_step
from platform_service.services.prompts.symbol_verbalization import (
    render_locale_list_map_field_schema,
    render_locale_map_field_schema,
)
from platform_service.services.quiz_explanation_sanitizer import (
    sanitize_explanation_localized,
)

logger = logging.getLogger(__name__)

QUIZ_GENERATION_TEMPLATE_ID = "post-publish-quiz-generation"
# v3: monolingual deployment — primary locale only.
QUIZ_GENERATION_TEMPLATE_VERSION = 4


def render_system_prompt(
    *,
    deployment_primary_locale: str | None = None,
    deployment_region_context: str | None = None,
) -> str:
    settings = get_settings()
    primary_locale = deployment_primary_locale or settings.deployment_primary_locale
    region_context = deployment_region_context or settings.deployment_region_context

    return f"""\
You write scenario-based quiz questions for community health workers (CHWs)
in {region_context}, using locale-keyed maps for all translatable fields.
The questions are delivered by a low-spec mobile app — they cannot use rich
media, only text.

Rules:
- One quiz item per question_index. Do NOT cluster sub-questions.
- Each question references content present in the supplied module cards.
  Do not invent facts.
- Single-select questions only (no multi-select, no ordering).
- 4 options per question. Exactly one is correct.
- Distractors must be plausibly wrong — content the CHW could reasonably
  pick if they had not internalised the card. No silly distractors.
- All translatable fields use the deployment primary locale ({primary_locale}).
- Explanations must stand alone: explain why the correct answer is correct in
  clinical prose. Do NOT mention card numbers, card titles, or phrases like
  "see Card N" / "কার্ড N". The `primary_card_index` field records which card
  the question tests — do not echo it in `explanation`.

Return STRICT JSON. The output must be a single object with this shape:
{{
  "questions": [
    {{
{render_locale_map_field_schema("case_setup", primary_locale=primary_locale, description="patient case, ~2 sentences")}
{render_locale_map_field_schema("question", primary_locale=primary_locale, primary_required=True)}
{render_locale_list_map_field_schema("options", primary_locale=primary_locale, max_items=4, description="exactly 4 options")}
      "correct_index": integer (0-3),
{render_locale_map_field_schema("explanation", primary_locale=primary_locale, description="why the correct answer is correct; no card references")}
      "primary_card_index": integer (1-based card number this question tests),
      "difficulty": "easy" | "moderate" | "hard"
    }},
    ...
  ]
}}

Do not include markdown fences or commentary. Only the JSON object.
"""


_HUMAN_TEMPLATE = """\
Module title: {module_title}
Module domain: {domain}
Estimated quiz size: {quiz_size} questions

## CARDS ##
{cards_block}
"""


def _localized_field_text(
    card: dict[str, Any],
    field: str,
    locale: str,
    *,
    settings: Settings | None = None,
) -> str | None:
    """Read plain text from a locale-keyed card field."""
    s = settings or get_settings()
    primary = deployment_locales(s)
    migrated = migrate_legacy_card(dict(card), primary=primary)
    localized = migrated.get(field)
    if not isinstance(localized, dict):
        return None
    value = localized.get(locale)
    if value is None:
        return None
    if field == "body":
        text = card_body_plain_text(value)
    else:
        text = str(value).strip() if value else ""
    return text or None


def _format_card_block(
    card: dict[str, Any],
    idx: int,
    *,
    primary_locale: str,
    settings: Settings,
) -> str:
    parts = [f"### Card {idx}"]
    title = _localized_field_text(card, "title", primary_locale, settings=settings)
    if title:
        parts.append(f"Title ({primary_locale}): {title}")
    body = _localized_field_text(card, "body", primary_locale, settings=settings)
    if body:
        parts.append(f"Body ({primary_locale}): {body}")
    for field in (
        "next_action",
        "previous_practice",
        "current_practice",
        "rationale_for_change",
    ):
        text = _localized_field_text(card, field, primary_locale, settings=settings)
        if text:
            label = field.replace("_", " ")
            parts.append(f"{label.title()} ({primary_locale}): {text}")
    return "\n".join(parts)


def _extract_localized_string(
    raw: dict[str, Any],
    field: str,
    *,
    settings: Settings,
) -> dict[str, str]:
    return extract_localized_string_from_raw(raw, field, settings=settings)


def _extract_localized_options(
    raw: dict[str, Any],
    *,
    settings: Settings,
) -> dict[str, list[Any]]:
    return extract_localized_options_from_raw(raw, settings=settings)


async def generate_quiz_for_module(module_id: UUID, *, step_id: UUID | None = None) -> int:
    """Generate quiz questions for a published module. Returns count written.

    Idempotent on retry — existing rows for `module_id` are deleted before
    writing the new batch. The dashboard surfaces "regenerate quiz" as a
    user action that calls this same path.
    """
    written = 0
    try:
        async with SessionLocal() as session:
            module = await _load_module(session, module_id)
            if module is None:
                logger.warning("Quiz worker: module %s not found", module_id)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=False,
                    error={"type": "ModuleNotFound", "message": f"module {module_id} not found"},
                )
                return 0
            cards = (module.module_json or {}).get("cards", [])
            if not cards:
                logger.info("Quiz worker: module %s has no cards; skipping quiz", module_id)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=True,
                    output_summary={"questions_written": 0},
                )
                return 0

            settings = get_settings()
            primary = deployment_locales(settings)
            module_title = localized_primary_text(module.title_localized, primary) or ""

            quiz_size = _target_quiz_size(len(cards))
            questions = await _call_llm(
                module_id=module_id,
                module_title=module_title,
                domain=module.domain,
                cards=cards,
                quiz_size=quiz_size,
                settings=settings,
            )
            if not questions:
                logger.warning("Quiz worker: LLM returned no usable questions for module %s", module_id)
                await finish_post_publish_step(
                    step_id=step_id,
                    success=True,
                    output_summary={"questions_written": 0},
                )
                return 0

            # Wipe any previous quiz rows for this module so retries don't pile up.
            result = await session.execute(
                select(ModuleQuizQuestion).where(ModuleQuizQuestion.module_id == module_id)
            )
            for existing in result.scalars().all():
                await session.delete(existing)
            await session.flush()

            for idx, q in enumerate(questions, start=1):
                question_localized = _extract_localized_string(q, "question", settings=settings)
                if not question_localized.get(primary):
                    continue
                row = ModuleQuizQuestion(
                    module_id=module_id,
                    question_order=idx,
                    question_family_id=uuid.uuid4(),
                    question_version=1,
                    case_setup_localized=_extract_localized_string(q, "case_setup", settings=settings)
                    or None,
                    question_localized=question_localized,
                    question_type="single_select",
                    options_localized=_extract_localized_options(q, settings=settings),
                    correct_indices=[int(q.get("correct_index", 0))],
                    explanation_localized=sanitize_explanation_localized(
                        _extract_localized_string(q, "explanation", settings=settings)
                    ),
                    difficulty=q.get("difficulty", "moderate"),
                )
                session.add(row)
                written += 1
            await session.commit()
            logger.info("Quiz worker: module %s wrote %d questions", module_id, written)
        await finish_post_publish_step(
            step_id=step_id,
            success=True,
            output_summary={"questions_written": written},
        )
        return written
    except Exception as exc:
        logger.exception("Quiz worker: unhandled error for module %s", module_id)
        await finish_post_publish_step(
            step_id=step_id,
            success=False,
            error={"type": type(exc).__name__, "message": str(exc)[:500]},
        )
        raise


def _target_quiz_size(card_count: int) -> int:
    """Pick a quiz size. Stays inside settings.quiz_min/max bounds, biased
    toward 1 question per card up to the maximum."""
    settings = get_settings()
    return max(settings.quiz_min_questions, min(card_count, settings.quiz_max_questions))


async def _load_module(session: AsyncSession, module_id: UUID) -> Module | None:
    return await session.get(Module, module_id)


async def _call_llm(
    *,
    module_id: UUID,
    module_title: str,
    domain: str,
    cards: list[dict[str, Any]],
    quiz_size: int,
    settings: Settings,
) -> list[dict[str, Any]]:
    primary = deployment_locales(settings)
    client = get_ai_client()
    cards_block = "\n\n".join(
        _format_card_block(c, i, primary_locale=primary, settings=settings)
        for i, c in enumerate(cards, start=1)
    )
    human_message = _HUMAN_TEMPLATE.format(
        module_title=module_title,
        domain=domain,
        quiz_size=quiz_size,
        cards_block=cards_block,
    )
    request = InferenceRequest(
        request_id=str(uuid.uuid4()),
        generation_type=GenerationType.QUIZ_DRAFTING,
        model_policy=ModelPolicy(model=settings.text_model),
        prompt=PromptSpec(
            template_id=QUIZ_GENERATION_TEMPLATE_ID,
            template_version=QUIZ_GENERATION_TEMPLATE_VERSION,
            resolved_system_prompt=render_system_prompt(
                deployment_primary_locale=primary,
                deployment_region_context=settings.deployment_region_context,
            ),
            resolved_human_message=human_message,
        ),
        constraints=GenerationConstraints(
            language=primary,
            output_format="json",
        ),
        trace_context=TraceContext(),
    )
    response = await client.generate(request)
    if response.error:
        logger.error("Quiz worker: ai-runtime error for module %s: %s", module_id, response.error)
        return []
    try:
        payload = resolve_parsed_json(response)
    except json.JSONDecodeError as exc:
        logger.error("Quiz worker: LLM output not JSON for module %s: %s", module_id, exc)
        return []
    if isinstance(payload, dict):
        questions = payload.get("questions", [])
    elif isinstance(payload, list):
        questions = payload
    else:
        logger.error("Quiz worker: unexpected payload shape for module %s", module_id)
        return []
    return [q for q in questions if isinstance(q, dict)]


__all__ = ["generate_quiz_for_module"]
