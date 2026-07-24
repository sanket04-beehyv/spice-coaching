"""Unit tests for coaching RAG JSON helpers (no DB / ai-runtime)."""

from __future__ import annotations

import pytest
from platform_service.services.coaching_rag_errors import CoachingRagError
from platform_service.services.coaching_rag_service import CoachingRagService, parse_rag_json
from platform_service.services.llm_text_utils import strip_json_fence


def test_strip_json_fence_removes_markdown() -> None:
    raw = '```json\n{"answer": "ok", "cited_module_ids": []}\n```'
    stripped = strip_json_fence(raw)
    assert stripped.startswith("{")
    assert "answer" in stripped


def test_parse_rag_json_accepts_pre_parsed_dict() -> None:
    out = parse_rag_json("ignored", {"answer": "x", "cited_module_ids": []})
    assert out["answer"] == "x"


def test_parse_rag_json_parses_raw_string() -> None:
    out = parse_rag_json('{"answer": "y", "cited_module_ids": []}', None)
    assert out["answer"] == "y"


def test_parse_rag_json_raises_on_garbage() -> None:
    with pytest.raises(CoachingRagError, match="non-JSON answer"):
        parse_rag_json("not json {{{", None)


class TestParseSuggestedQuestions:
    def test_valid_list(self) -> None:
        out = CoachingRagService._parse_suggested_questions(["  First?  ", "Second?", "Third?"])
        assert out == ["First?", "Second?", "Third?"]

    def test_strips_and_drops_empty(self) -> None:
        out = CoachingRagService._parse_suggested_questions(["  ok  ", "", "   ", 42, None])
        assert out == ["ok"]

    def test_dedupes_case_insensitive(self) -> None:
        out = CoachingRagService._parse_suggested_questions(["What?", "what?", "WHAT?"])
        assert out == ["What?"]

    def test_caps_at_max_count(self) -> None:
        raw = [f"Q{i}?" for i in range(10)]
        out = CoachingRagService._parse_suggested_questions(raw, max_count=5)
        assert len(out) == 5
        assert out == [f"Q{i}?" for i in range(5)]

    def test_non_list_returns_empty(self) -> None:
        assert CoachingRagService._parse_suggested_questions(None) == []
        assert CoachingRagService._parse_suggested_questions("not a list") == []
        assert CoachingRagService._parse_suggested_questions({}) == []
