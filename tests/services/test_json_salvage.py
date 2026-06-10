"""Unit tests for platform JSON salvage helpers."""

from __future__ import annotations

import json

from platform_service.services._json_salvage import (
    extract_inner_array,
    salvage_truncated_array,
)


class TestSalvageTruncatedArray:
    def test_complete_array_returns_none(self) -> None:
        raw = '[{"a": 1}, {"b": 2}]'
        assert salvage_truncated_array(raw) is None

    def test_recovers_complete_elements_before_truncation(self) -> None:
        raw = '[{"id": 1, "title": "one"}, {"id": 2, "title": "two"}, {"id": 3, "tit'
        result = salvage_truncated_array(raw)
        assert result is not None
        assert len(result) == 2
        assert result[0]["id"] == 1
        assert result[1]["id"] == 2

    def test_returns_none_when_first_element_incomplete(self) -> None:
        raw = '[{"id": 1, "tit'
        assert salvage_truncated_array(raw) is None

    def test_returns_none_for_non_array_input(self) -> None:
        assert salvage_truncated_array('{"items": []}') is None

    def test_handles_strings_with_braces_inside(self) -> None:
        raw = '[{"text": "brace } inside"}, {"id": 2}, {"id": 3, "x'
        result = salvage_truncated_array(raw)
        assert result is not None
        assert len(result) == 2
        assert "brace } inside" in result[0]["text"]


class TestExtractInnerArray:
    def test_finds_array_after_key(self) -> None:
        raw = '{"candidates": [{"id": 1}, {"id": 2'
        inner = extract_inner_array(raw, key="candidates")
        assert inner is not None
        assert inner.startswith("[")
        salvaged = salvage_truncated_array(inner)
        assert salvaged is not None
        assert len(salvaged) == 1

    def test_returns_none_when_key_missing(self) -> None:
        assert extract_inner_array('{"other": []}', key="candidates") is None

    def test_returns_none_when_no_bracket_after_key(self) -> None:
        assert extract_inner_array('{"candidates": null}', key="candidates") is None


class TestWrappedSalvageFlow:
    def test_consolidated_candidates_wrapper(self) -> None:
        raw = (
            '{"consolidated_candidates": [{"gap_code": "a"}, {"gap_code": "b"}, '
            '{"gap_code": "c", "rationale": "trunc'
        )
        inner = extract_inner_array(raw, key="consolidated_candidates")
        assert inner is not None
        result = salvage_truncated_array(inner)
        assert result is not None
        assert [item["gap_code"] for item in result] == ["a", "b"]
        # Salvaged JSON must parse.
        json.loads(json.dumps(result))
