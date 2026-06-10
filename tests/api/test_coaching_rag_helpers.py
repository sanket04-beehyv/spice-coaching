"""Unit tests for coaching RAG JSON helpers (no DB / ai-runtime)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from platform_service.services.coaching_rag_service import parse_rag_json
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
    with pytest.raises(HTTPException) as exc:
        parse_rag_json("not json {{{", None)
    assert exc.value.status_code == 502
