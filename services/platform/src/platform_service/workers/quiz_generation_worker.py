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
    TraceContext,
)
from mc_foundation.locale import localized_primary_text
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.repositories.module_read_repository import ModuleReadRepository
from platform_service.deps import get_ai_client
from platform_service.localized import (
    deployment_locales,
    extract_localized_options_from_raw,
    extract_localized_string_from_raw,
    migrate_legacy_card,
)
from platform_service.services.card_body_text import card_body_plain_text
from platform_service.services.card_normalisation import card_row_to_dict
from platform_service.services.llm_response_resolver import resolve_parsed_json
from platform_service.services.post_publish_step import finish_post_publish_step
from platform_service.services.prompt_registry import QUIZ_GENERATION_TEMPLATE_ID
from platform_service.services.prompt_template_service import PromptTemplateService, prompt_spec_from_rendered
from platform_service.services.prompt_variables.quiz_generation_variables import (
    build_quiz_generation_variables,
)
from platform_service.services.quiz_explanation_sanitizer import (
    sanitize_explanation_localized,
)

logger = logging.getLogger(__name__)

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


async def generate_quiz_for_module(
    module_id: UUID,
    *,
    step_id: UUID | None = None,
    quiz_size: int | None = None,
) -> int:
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
            card_rows = await ModuleReadRepository(session).list_cards(module_id)
            cards = [card_row_to_dict(row) for row in card_rows]
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

            resolved_quiz_size = (
                _clamp_quiz_size(quiz_size, settings=settings)
                if quiz_size is not None
                else _target_quiz_size(len(cards))
            )
            questions = await _call_llm(
                module_id=module_id,
                module_title=module_title,
                domain=module.domain,
                cards=cards,
                quiz_size=resolved_quiz_size,
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

            card_family_by_index = {
                idx: UUID(str(card["card_family_id"]))
                for idx, card in enumerate(cards, start=1)
                if card.get("card_family_id")
            }

            for idx, q in enumerate(questions, start=1):
                question_localized = _extract_localized_string(q, "question", settings=settings)
                if not question_localized.get(primary):
                    continue
                primary_card_index = q.get("primary_card_index")
                primary_card_family_id = None
                if isinstance(primary_card_index, int):
                    primary_card_family_id = card_family_by_index.get(primary_card_index)

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
                    primary_card_family_id=primary_card_family_id,
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


def _clamp_quiz_size(quiz_size: int, *, settings: Settings) -> int:
    """Clamp an explicit ingest target to deployment quiz bounds."""
    return max(settings.quiz_min_questions, min(quiz_size, settings.quiz_max_questions))


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
    rendered = await PromptTemplateService().render(
        None,
        template_id=QUIZ_GENERATION_TEMPLATE_ID,
        variant_key=None,
        variables=build_quiz_generation_variables(
            module_title=module_title,
            domain=domain,
            quiz_size=quiz_size,
            cards_block=cards_block,
            deployment_primary_locale=primary,
            deployment_region_context=settings.deployment_region_context,
            settings=settings,
        ),
    )
    request = InferenceRequest(
        request_id=str(uuid.uuid4()),
        generation_type=GenerationType.QUIZ_DRAFTING,
        prompt=prompt_spec_from_rendered(rendered),
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
