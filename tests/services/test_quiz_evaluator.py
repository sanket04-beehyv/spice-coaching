"""W-10 — quiz_evaluator pure-unit tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.services.quiz_evaluator import QuizAnswer, score_quiz


def _qid() -> object:
    return uuid4()


def test_all_correct_passes_with_score_1() -> None:
    q1, q2, q3 = _qid(), _qid(), _qid()
    out = score_quiz(
        answers=[
            QuizAnswer(question_family_id=q1, selected_indices=[0]),
            QuizAnswer(question_family_id=q2, selected_indices=[1]),
            QuizAnswer(question_family_id=q3, selected_indices=[2]),
        ],
        correct_by_question={q1: [0], q2: [1], q3: [2]},
        pass_threshold=0.7,
    )
    assert out.correct_count == 3
    assert out.total_count == 3
    assert out.score_pct == pytest.approx(1.0)
    assert out.passed is True
    assert out.missed_question_family_ids == []
    assert out.unanswered_question_family_ids == []


def test_score_at_threshold_passes() -> None:
    """Edge case: 70% exactly should pass with default threshold 0.70."""
    qids = [_qid() for _ in range(10)]
    correct = {q: [0] for q in qids}
    answers = [QuizAnswer(question_family_id=q, selected_indices=[0]) for q in qids[:7]]
    answers += [QuizAnswer(question_family_id=q, selected_indices=[1]) for q in qids[7:]]
    out = score_quiz(answers=answers, correct_by_question=correct, pass_threshold=0.7)
    assert out.score_pct == pytest.approx(0.7)
    assert out.passed is True


def test_score_just_below_threshold_fails() -> None:
    """Edge case: 69% (one wrong out of ten with thr=0.7 → 0.6) fails."""
    qids = [_qid() for _ in range(10)]
    correct = {q: [0] for q in qids}
    answers = [QuizAnswer(question_family_id=q, selected_indices=[0]) for q in qids[:6]]
    answers += [QuizAnswer(question_family_id=q, selected_indices=[1]) for q in qids[6:]]
    out = score_quiz(answers=answers, correct_by_question=correct, pass_threshold=0.7)
    assert out.score_pct == pytest.approx(0.6)
    assert out.passed is False
    assert len(out.missed_question_family_ids) == 4


def test_empty_answers_returns_zero_and_fail() -> None:
    qids = [_qid(), _qid()]
    correct = {q: [0] for q in qids}
    out = score_quiz(answers=[], correct_by_question=correct)
    assert out.correct_count == 0
    assert out.score_pct == 0.0
    assert out.passed is False
    assert sorted(out.unanswered_question_family_ids) == sorted(qids)


def test_unanswered_questions_count_against_score() -> None:
    q1, q2, q3 = _qid(), _qid(), _qid()
    out = score_quiz(
        answers=[QuizAnswer(question_family_id=q1, selected_indices=[0])],
        correct_by_question={q1: [0], q2: [0], q3: [0]},
        pass_threshold=0.5,
    )
    assert out.correct_count == 1
    assert out.total_count == 3
    assert out.score_pct == pytest.approx(1 / 3)
    assert out.passed is False
    assert q2 in out.unanswered_question_family_ids
    assert q3 in out.unanswered_question_family_ids


def test_multi_select_correct_only_when_indices_match_exactly() -> None:
    q = _qid()
    correct = {q: [0, 2]}
    # Wrong: missing one
    out = score_quiz(
        answers=[QuizAnswer(question_family_id=q, selected_indices=[0])],
        correct_by_question=correct,
    )
    assert out.correct_count == 0
    # Wrong: extra
    out = score_quiz(
        answers=[QuizAnswer(question_family_id=q, selected_indices=[0, 1, 2])],
        correct_by_question=correct,
    )
    assert out.correct_count == 0
    # Right: same set in any order
    out = score_quiz(
        answers=[QuizAnswer(question_family_id=q, selected_indices=[2, 0])],
        correct_by_question=correct,
    )
    assert out.correct_count == 1


def test_unknown_question_id_in_answer_is_ignored_with_warning() -> None:
    q1 = _qid()
    bogus = _qid()
    out = score_quiz(
        answers=[
            QuizAnswer(question_family_id=q1, selected_indices=[0]),
            QuizAnswer(question_family_id=bogus, selected_indices=[0]),
        ],
        correct_by_question={q1: [0]},
        pass_threshold=0.5,
    )
    assert out.correct_count == 1
    assert out.total_count == 1  # bogus question not counted
    assert out.passed is True
    assert out.ignored_answer_count == 1


def test_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError, match="pass_threshold"):
        score_quiz(answers=[], correct_by_question={}, pass_threshold=1.5)
    with pytest.raises(ValueError, match="pass_threshold"):
        score_quiz(answers=[], correct_by_question={}, pass_threshold=-0.1)


def test_duplicate_answer_for_same_qid_uses_last() -> None:
    """Defensive: if SDK sends two answers for the same qid (re-answered),
    the last one wins (matches the SDK's overwrite semantics)."""
    q = _qid()
    out = score_quiz(
        answers=[
            QuizAnswer(question_family_id=q, selected_indices=[1]),  # wrong first
            QuizAnswer(question_family_id=q, selected_indices=[0]),  # correct second
        ],
        correct_by_question={q: [0]},
    )
    assert out.correct_count == 1
    assert out.passed is True
