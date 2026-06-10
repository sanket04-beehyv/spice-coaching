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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.deps import get_ai_client
from platform_service.services.llm_response_resolver import resolve_parsed_json
from platform_service.services.post_publish_step import finish_post_publish_step

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """\
You write scenario-based quiz questions for community health workers (CHWs)
in rural Bangladesh, in Bangla (with English mirror). The questions are
delivered by a low-spec mobile app — they cannot use rich media, only text.

Rules:
- One quiz item per question_index. Do NOT cluster sub-questions.
- Each question references content present in the supplied module cards.
  Do not invent facts.
- Single-select questions only (no multi-select, no ordering).
- 4 options per question. Exactly one is correct.
- Distractors must be plausibly wrong — content the CHW could reasonably
  pick if they had not internalised the card. No silly distractors.
- Bangla is canonical; English mirror is for reviewer/admin readability.
- Explanations cite which card the answer comes from (1-indexed card number).

Return STRICT JSON. The output must be a single object with this shape:
{
  "questions": [
    {
      "case_setup_bn": "string or null — patient case in Bangla, ~2 sentences",
      "case_setup_en": "string or null — English mirror",
      "question_bn": "string — Bangla question, required",
      "question_en": "string or null — English mirror",
      "options_bn": ["...", "...", "...", "..."],
      "options_en": ["...", "...", "...", "..."],
      "correct_index": integer (0-3),
      "explanation_bn": "string — why the correct answer is correct, in Bangla",
      "explanation_en": "string or null — English mirror",
      "primary_card_index": integer (1-based card number this question tests),
      "difficulty": "easy" | "moderate" | "hard"
    },
    ...
  ]
}

Do not include markdown fences or commentary. Only the JSON object.
"""

_HUMAN_TEMPLATE = """\
Module title: {title_bn}
Module domain: {domain}
Estimated quiz size: {quiz_size} questions

## CARDS ##
{cards_block}
"""


def _format_card_block(card: dict[str, Any], idx: int) -> str:
    parts = [f"### Card {idx}"]
    if card.get("title_bn"):
        parts.append(f"Title (bn): {card['title_bn']}")
    if card.get("body_bn"):
        parts.append(f"Body (bn): {card['body_bn']}")
    if card.get("next_action_bn"):
        parts.append(f"Next action (bn): {card['next_action_bn']}")
    if card.get("previous_practice_bn"):
        parts.append(f"Previous practice (bn): {card['previous_practice_bn']}")
    if card.get("current_practice_bn"):
        parts.append(f"Current practice (bn): {card['current_practice_bn']}")
    if card.get("rationale_for_change_bn"):
        parts.append(f"Rationale (bn): {card['rationale_for_change_bn']}")
    return "\n".join(parts)


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

            quiz_size = _target_quiz_size(len(cards))
            questions = await _call_llm(
                module_id=module_id,
                title_bn=module.title_bn or "",
                domain=module.domain,
                cards=cards,
                quiz_size=quiz_size,
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
                row = ModuleQuizQuestion(
                    module_id=module_id,
                    question_order=idx,
                    question_family_id=uuid.uuid4(),
                    question_version=1,
                    case_setup_en=q.get("case_setup_en"),
                    case_setup_bn=q.get("case_setup_bn"),
                    question_en=q.get("question_en"),
                    question_bn=q.get("question_bn") or "",
                    question_type="single_select",
                    options_en=q.get("options_en"),
                    options_bn=q.get("options_bn") or [],
                    correct_indices=[int(q.get("correct_index", 0))],
                    explanation_en=q.get("explanation_en"),
                    explanation_bn=q.get("explanation_bn"),
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
    title_bn: str,
    domain: str,
    cards: list[dict[str, Any]],
    quiz_size: int,
) -> list[dict[str, Any]]:
    settings = get_settings()
    client = get_ai_client()
    cards_block = "\n\n".join(_format_card_block(c, i) for i, c in enumerate(cards, start=1))
    human_message = _HUMAN_TEMPLATE.format(
        title_bn=title_bn,
        domain=domain,
        quiz_size=quiz_size,
        cards_block=cards_block,
    )
    request = InferenceRequest(
        request_id=str(uuid.uuid4()),
        generation_type=GenerationType.QUIZ_DRAFTING,
        model_policy=ModelPolicy(model=settings.text_model),
        prompt=PromptSpec(
            template_id="post-publish-quiz-generation",
            template_version=1,
            resolved_system_prompt=_SYSTEM_PROMPT,
            resolved_human_message=human_message,
        ),
        constraints=GenerationConstraints(
            language="bn",
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
