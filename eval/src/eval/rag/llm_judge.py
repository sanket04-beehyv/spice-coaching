"""LLM-as-judge scoring for RAG evaluation."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from uuid import UUID

from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    InferenceResponse,
    PromptSpec,
    TraceContext,
)
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.services.llm_text_utils import strip_json_fence

from eval.rag.corpus import CardCorpusDoc

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM = (
    "You are an expert evaluator for a clinical CHW training RAG chatbot. "
    "Score the model answer using ONLY the provided MODULE excerpts as ground truth. "
    "Respond with a single JSON object, no markdown fences, keys:\n"
    '- "faithfulness": float 0.0–1.0 — claims in the answer are supported by excerpts\n'
    '- "answer_relevance": float 0.0–1.0 — answer addresses the user question\n'
    '- "groundedness": float 0.0–1.0 — no facts outside the excerpts\n'
    "Use 1.0 for fully correct, 0.0 for completely wrong. Partial credit is allowed."
)


@dataclass(frozen=True)
class JudgeScores:
    faithfulness: float | None
    answer_relevance: float | None
    groundedness: float | None
    judge_error: str | None = None

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "faithfulness": self.faithfulness,
            "answer_relevance": self.answer_relevance,
            "groundedness": self.groundedness,
            "judge_error": self.judge_error,
        }


def _card_display_title(card: CardCorpusDoc) -> str:
    return card.primary_title or card.title_en or card.title_bn or ""


def build_judge_context(
    retrieved_module_ids: list[UUID],
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
    *,
    max_chars: int,
) -> str:
    blocks: list[str] = []
    used = 0
    for module_id in retrieved_module_ids:
        cards = cards_by_module.get(module_id, [])
        if not cards:
            continue
        header = f"=== MODULE {module_id} ===\n"
        if used + len(header) > max_chars:
            break
        blocks.append(header)
        used += len(header)
        for card in cards:
            title = _card_display_title(card)
            chunk = f"--- {title} ---\n{card.text}\n"
            if used + len(chunk) > max_chars:
                blocks.append("... truncated (char budget)\n")
                return "\n".join(blocks)
            blocks.append(chunk)
            used += len(chunk)
    return "\n".join(blocks)


def _clamp_score(value: object) -> float | None:
    if value is None:
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, score))


def parse_judge_response(response: InferenceResponse) -> JudgeScores:
    payload: dict[str, object] | None = None
    if isinstance(response.parsed_json, dict):
        payload = response.parsed_json
    elif response.raw_text:
        try:
            payload = json.loads(strip_json_fence(response.raw_text))
        except json.JSONDecodeError as exc:
            return JudgeScores(
                faithfulness=None,
                answer_relevance=None,
                groundedness=None,
                judge_error=f"json parse failed: {exc}",
            )

    if not payload:
        return JudgeScores(
            faithfulness=None,
            answer_relevance=None,
            groundedness=None,
            judge_error="empty judge response",
        )

    return JudgeScores(
        faithfulness=_clamp_score(payload.get("faithfulness")),
        answer_relevance=_clamp_score(payload.get("answer_relevance")),
        groundedness=_clamp_score(payload.get("groundedness")),
    )


class LlmJudge:
    """Score RAG answers with a separate LLM judge call via ai-runtime."""

    def __init__(
        self,
        client: AIRuntimeClient,
        *,
        max_context_chars: int = 12_000,
    ) -> None:
        self._client = client
        self._max_context_chars = max_context_chars

    async def score(
        self,
        *,
        question: str,
        answer: str,
        retrieved_module_ids: list[UUID],
        cards_by_module: dict[UUID, list[CardCorpusDoc]],
    ) -> JudgeScores:
        if not answer.strip():
            return JudgeScores(
                faithfulness=None,
                answer_relevance=None,
                groundedness=None,
                judge_error="empty answer",
            )

        context = build_judge_context(
            retrieved_module_ids,
            cards_by_module,
            max_chars=self._max_context_chars,
        )
        if not context.strip():
            return JudgeScores(
                faithfulness=None,
                answer_relevance=None,
                groundedness=None,
                judge_error="no context for judge",
            )

        human = (
            f"USER_QUESTION:\n{question}\n\n"
            f"MODULE_EXCERPTS:\n{context}\n\n"
            f"MODEL_ANSWER:\n{answer}\n\n"
            "Return JSON only."
        )
        request = InferenceRequest(
            request_id=str(uuid.uuid4()),
            generation_type=GenerationType.RAG_EVAL_JUDGE,
            prompt=PromptSpec(
                template_id="rag_eval_judge_v1",
                template_version=1,
                resolved_system_prompt=_JUDGE_SYSTEM,
                resolved_human_message=human,
            ),
            constraints=GenerationConstraints(
                output_format="json",
            ),
            trace_context=TraceContext(),
            context={"question": question},
        )
        try:
            response = await self._client.generate(request)
        except Exception as exc:
            logger.warning("llm judge call failed: %s", exc)
            return JudgeScores(
                faithfulness=None,
                answer_relevance=None,
                groundedness=None,
                judge_error=str(exc),
            )

        if response.error:
            return JudgeScores(
                faithfulness=None,
                answer_relevance=None,
                groundedness=None,
                judge_error=response.error,
            )

        return parse_judge_response(response)
