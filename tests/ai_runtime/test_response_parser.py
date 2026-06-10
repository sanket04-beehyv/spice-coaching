"""ai-runtime response_parser tests — including the smoke-loop regression.

The Layer 1 smoke run on a 168-page BRAC SK PDF uncovered that Gemini 2.5
Pro can hit its `max_output_tokens` budget mid-token, leaving the JSON
array unterminated. Without a salvage path, our balanced-bracket fallback
silently picked the first nested `{...}` object and reported "1 candidate"
instead of failing loudly. The fixes pinned here:

1. Direct parse uses `strict=False` so a literal control character inside
   a string doesn't kill the parse.
2. When the response begins with `[` but never closes, we salvage every
   complete element before the truncation point instead of falling back to
   "first nested object".
3. The salvage returns a `list`, not a `dict`, so downstream callers like
   `module_identifier._extract_candidates` see the right shape.
"""

from __future__ import annotations

import json

from ai_runtime.services.response_parser import extract_json


class TestDirectParseClean:
    def test_object(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_array(self) -> None:
        assert extract_json('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]

    def test_with_leading_whitespace(self) -> None:
        assert extract_json("\n\n  [1, 2, 3]  \n") == [1, 2, 3]


class TestMarkdownFence:
    def test_strips_json_fence(self) -> None:
        wrapped = 'preamble\n```json\n{"k": "v"}\n```\nepilogue'
        assert extract_json(wrapped) == {"k": "v"}

    def test_strips_bare_fence(self) -> None:
        wrapped = "```\n[1, 2]\n```"
        assert extract_json(wrapped) == [1, 2]


class TestStrictFalse:
    def test_literal_newline_in_string_is_tolerated(self) -> None:
        """Gemini occasionally emits a literal `\\n` inside a string value
        instead of the escape sequence `\\\\n`. Standard `json.loads(strict=True)`
        rejects that; we pass `strict=False` so it parses."""
        # Build a string with a real newline character inside the string value.
        bad = '{"description": "line1\nline2"}'
        result = extract_json(bad)
        assert result == {"description": "line1\nline2"}


class TestTruncationSalvage:
    def test_truncated_array_recovers_complete_elements(self) -> None:
        """Mimic the BRAC-SK Stage 2 failure: top-level array, two complete
        candidates, third candidate cut off mid-string."""
        truncated = (
            "[\n"
            '  {"proposed_title": "Module A", "blocks": ["b1", "b2"]},\n'
            '  {"proposed_title": "Module B", "blocks": ["b3"]},\n'
            '  {"proposed_title": "Module C", "blocks": ["b4-cut'
        )
        result = extract_json(truncated)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["proposed_title"] == "Module A"
        assert result[1]["proposed_title"] == "Module B"

    def test_truncated_array_with_zero_complete_elements_returns_none(self) -> None:
        """If even the first element didn't finish, salvage returns None
        rather than an empty list."""
        truncated = '[\n  {"proposed_title": "Cut'
        assert extract_json(truncated) is None

    def test_array_with_unescaped_quote_in_string_falls_through_to_salvage(
        self,
    ) -> None:
        """One element has a syntax error mid-string. The strict balanced
        walk fails on it; salvage recovers earlier complete elements."""
        broken = (
            "[\n"
            '  {"k": "v1"},\n'
            '  {"k": "v2"},\n'
            '  {"k": "broke" garbage here'  # never closes, not balanced
        )
        result = extract_json(broken)
        # Either salvage returns the 2 complete elements, OR the parser
        # gives up and returns None — what we MUST NOT do is silently
        # collapse to the first object.
        assert result is None or (isinstance(result, list) and len(result) == 2)
        assert not isinstance(result, dict)

    def test_completed_array_is_not_salvaged(self) -> None:
        """If the array is well-formed, the direct parse path wins; salvage
        never runs. Verify by ensuring the result is the full array."""
        good = '[{"a": 1}, {"b": 2}, {"c": 3}]'
        result = extract_json(good)
        assert result == [{"a": 1}, {"b": 2}, {"c": 3}]


class TestNoJsonAtAll:
    def test_returns_none_when_no_json_present(self) -> None:
        assert extract_json("just some prose, no JSON here.") is None

    def test_returns_none_for_empty(self) -> None:
        assert extract_json("") is None
        assert extract_json("   \n  ") is None


class TestRoundTripWithEscapesPreserved:
    """Sanity check that strict=False doesn't mangle properly-escaped JSON."""

    def test_escape_sequences_intact(self) -> None:
        original = {"path": "C:\\Users\\foo", "newline": "a\nb", "quote": 'say "hi"'}
        text = json.dumps(original)
        assert extract_json(text) == original
