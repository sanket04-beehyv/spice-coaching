"""Tests for shared chatbot question text helpers."""

from __future__ import annotations

from platform_service.services.question_text import normalize_question


def test_normalize_question_collapses_whitespace() -> None:
    assert normalize_question("  child   cough  ") == "child cough"


def test_normalize_question_casefolds() -> None:
    assert normalize_question("How Do I Measure?") == "how do i measure?"


def test_normalize_question_merges_case_and_whitespace_variants() -> None:
    assert normalize_question("Child  Cough") == normalize_question("child cough")
