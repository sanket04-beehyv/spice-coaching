"""W-10 — pure scoring logic for module quiz attempts.

The SDK delivers an attempted quiz as a list of (question_family_id,
selected_indices) pairs. This module:
- compares against the canonical correct_indices stored on
  ModuleQuizQuestion
- computes a percentage score
- decides pass/fail against a threshold (per-module override > settings
  default; per-module override is W-11 scope)
- returns the missed question_family_ids so the SDK can show targeted
  remediation in the result screen

No DB or network access — pure data in / data out. The
`module_completion_worker` calls this and then persists the outcome.

Edge cases (Implementation Plan W-10):
- Score exactly at threshold (default 0.70) → pass
- Score 1% below threshold (0.69) → fail (and trigger retrigger)
- All correct → pass with score=1.0
- Empty answers list → fail with score=0.0 (CHW abandoned mid-quiz)
- Answers reference an unknown question_family_id → that answer is ignored
  (logged); the canonical question count is the source of truth
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuizAnswer:
    """One answer the CHW submitted for one quiz question."""

    question_family_id: UUID
    selected_indices: list[int]


@dataclass(frozen=True)
class QuizScoreResult:
    """Outcome of scoring one quiz attempt."""

    correct_count: int
    total_count: int
    score_pct: float  # 0.0 – 1.0
    passed: bool
    pass_threshold: float
    missed_question_family_ids: list[UUID] = field(default_factory=list)
    unanswered_question_family_ids: list[UUID] = field(default_factory=list)
    ignored_answer_count: int = 0


def _normalise_indices(indices: Iterable[int]) -> tuple[int, ...]:
    """Sort + dedup so single-select and multi-select compare cleanly."""
    return tuple(sorted({int(i) for i in indices}))


def score_quiz(
    *,
    answers: Iterable[QuizAnswer],
    correct_by_question: Mapping[UUID, Iterable[int]],
    pass_threshold: float = 0.70,
) -> QuizScoreResult:
    """Score one quiz attempt.

    `correct_by_question` enumerates every question in the quiz (this is the
    denominator). `answers` may be a subset (e.g. CHW skipped some). An
    `answer` whose question_family_id isn't in `correct_by_question` is
    ignored with a warning — the SDK should never send those, but we don't
    want a single bad ID to crash the score.
    """
    if not 0.0 <= pass_threshold <= 1.0:
        raise ValueError(f"pass_threshold must be in [0.0, 1.0], got {pass_threshold!r}")

    correct_normalised: dict[UUID, tuple[int, ...]] = {
        qid: _normalise_indices(idx) for qid, idx in correct_by_question.items()
    }
    total_count = len(correct_normalised)

    answers_by_qid: dict[UUID, tuple[int, ...]] = {}
    ignored = 0
    for ans in answers:
        if ans.question_family_id not in correct_normalised:
            ignored += 1
            logger.warning(
                "Quiz score: answer for unknown question_family_id=%s ignored",
                ans.question_family_id,
            )
            continue
        # Last answer wins if the SDK sent the same qid twice.
        answers_by_qid[ans.question_family_id] = _normalise_indices(ans.selected_indices)

    correct_count = 0
    missed: list[UUID] = []
    unanswered: list[UUID] = []
    for qid, expected in correct_normalised.items():
        if qid not in answers_by_qid:
            unanswered.append(qid)
            continue
        if answers_by_qid[qid] == expected:
            correct_count += 1
        else:
            missed.append(qid)

    score_pct = (correct_count / total_count) if total_count > 0 else 0.0
    passed = total_count > 0 and score_pct >= pass_threshold

    return QuizScoreResult(
        correct_count=correct_count,
        total_count=total_count,
        score_pct=score_pct,
        passed=passed,
        pass_threshold=pass_threshold,
        missed_question_family_ids=missed,
        unanswered_question_family_ids=unanswered,
        ignored_answer_count=ignored,
    )
