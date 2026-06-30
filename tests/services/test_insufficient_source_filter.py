"""W-4 / Layer 2 — insufficient_source_filter unit tests.

After the architecture reset, this filter is **advisory**: stage_c_identify
records `fail_reasons` on `quality_flags_jsonb` instead of rejecting the
candidate. Tests pin both the heuristic logic AND the contract that
callers can use the result without it mutating their inputs.
"""

import copy
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from platform_service.config import get_settings
from platform_service.services.insufficient_source_filter import FilterDecision, evaluate_candidate


def _provenance(source_doc_id, source_page_id, *block_ids):
    return [
        {
            "source_document_id": str(source_doc_id),
            "source_page_id": str(source_page_id),
            "content_block_ids": [str(b) for b in block_ids],
        }
    ]


class TestPassingCandidates:
    def test_high_token_high_heading_passes(self) -> None:
        b1, b2, b3, b4 = uuid4(), uuid4(), uuid4(), uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1, b2, b3, b4)
        decision = evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 200, b2: 200, b3: 200, b4: 200},
            cited_block_headings={
                b1: ["Section A"],
                b2: ["Section B"],
                b3: ["Section C"],
                b4: ["Section D"],
            },
        )
        assert decision.accepted
        assert decision.total_tokens == 800
        assert decision.distinct_headings == 4

    def test_dedupes_blocks_across_provenance_entries(self) -> None:
        b1, b2 = uuid4(), uuid4()
        provenance = [
            {
                "source_document_id": str(uuid4()),
                "source_page_id": str(uuid4()),
                "content_block_ids": [str(b1), str(b2)],
            },
            # Same blocks repeated — should NOT double-count tokens
            {
                "source_document_id": str(uuid4()),
                "source_page_id": str(uuid4()),
                "content_block_ids": [str(b1), str(b2)],
            },
        ]
        decision = evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 300, b2: 300},
            cited_block_headings={b1: ["A"], b2: ["B"], (b1, "x"): []},
        )
        assert decision.total_tokens == 600  # not 1200


class TestFailingCandidates:
    def test_no_provenance_fails(self) -> None:
        decision = evaluate_candidate(
            source_provenance=[],
            cited_block_token_counts={},
            cited_block_headings={},
        )
        assert not decision.accepted
        assert "no_provenance" in decision.fail_reasons

    def test_below_token_threshold_fails(self) -> None:
        # Default min_tokens is 50 (config). 30 total is below it.
        b1, b2, b3 = uuid4(), uuid4(), uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1, b2, b3)
        decision = evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 10, b2: 10, b3: 10},
            cited_block_headings={b1: ["A"], b2: ["B"], b3: ["C"]},
            outline_section_count=10,
        )
        assert not decision.accepted
        assert "insufficient_tokens" in decision.fail_reasons

    def test_below_heading_threshold_fails(self) -> None:
        # Default min_headings is 1; to fail heading coverage we need
        # distinct_headings == 0 with a deep outline.
        b1, b2 = uuid4(), uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1, b2)
        decision = evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 400, b2: 400},
            cited_block_headings={b1: [], b2: []},  # zero distinct headings
            outline_section_count=10,  # deep outline → coverage check applies
        )
        assert not decision.accepted
        assert "insufficient_heading_coverage" in decision.fail_reasons


class TestShallowOutlineFallback:
    def test_shallow_outline_accepts_two_distinct_headings(self) -> None:
        """Per Pipeline §6: when outline itself is shallow, the precondition
        relaxes to ≥ 2 distinct sections instead of ≥ 3 headings."""
        b1, b2 = uuid4(), uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1, b2)
        decision = evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 400, b2: 400},
            cited_block_headings={b1: ["A"], b2: ["B"]},
            outline_section_count=2,  # shallow outline → fallback rule
        )
        assert decision.accepted

    def test_shallow_outline_still_fails_with_one_heading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The shallow path triggers when distinct_headings < min_headings AND
        outline_section_count > 0 AND outline_section_count < min_headings.
        With default min_headings=1 the elif is unreachable, so bump
        min_headings to 3 to exercise the shallow-outline branch."""
        monkeypatch.setattr(get_settings(), "stage_c_insufficient_source_min_headings", 3)

        b1 = uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1)
        decision = evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 800},
            cited_block_headings={b1: ["Only Heading"]},
            outline_section_count=2,  # shallow (< min_headings=3) and non-empty
        )
        assert not decision.accepted
        assert "insufficient_section_coverage" in decision.fail_reasons


class TestMalformedProvenance:
    def test_skips_non_dict_entries(self) -> None:
        provenance = ["not a dict", {"missing_keys": True}]
        decision = evaluate_candidate(
            source_provenance=provenance,  # type: ignore[arg-type]
            cited_block_token_counts={},
            cited_block_headings={},
        )
        # All entries are malformed → flat is empty → no_provenance
        assert not decision.accepted
        assert "no_provenance" in decision.fail_reasons

    def test_skips_invalid_uuids(self) -> None:
        provenance = [
            {
                "source_document_id": "not-a-uuid",
                "source_page_id": "also-not",
                "content_block_ids": [],
            }
        ]
        decision = evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={},
            cited_block_headings={},
        )
        assert not decision.accepted


# ── Layer 2 regressions for the P1 architecture-reset fix ─────────────────


class TestAdvisoryRoleAggregation:
    """Per the architecture reset, the filter is advisory: it must compute
    accurate flags so the caller (stage_c_identify) can write them onto
    `quality_flags_jsonb`. The pipeline never rejects on these flags
    anymore — but the flags themselves still need to be right.
    """

    def test_multiple_flags_aggregated_in_stable_order(self) -> None:
        """Tokens + headings both fail → both flags present, in code-path
        order (tokens before headings)."""
        b1 = uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1)
        decision = evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 5},  # below tokens
            cited_block_headings={b1: []},  # zero headings
            outline_section_count=10,  # outline is deep
        )
        assert decision.fail_reasons == (
            "insufficient_tokens",
            "insufficient_heading_coverage",
        )
        # And accepted is False — the boolean is preserved for legacy
        # callers, but stage_c_identify ignores it now.
        assert decision.accepted is False

    def test_decision_carries_full_metrics_for_dashboard(self) -> None:
        """The dashboard surfaces total_tokens + distinct_headings to
        reviewers. They must be populated even when the candidate is
        flagged, so reviewers see WHY (not just THAT) it was flagged."""
        b1, b2 = uuid4(), uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1, b2)
        decision = evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 7, b2: 3},
            cited_block_headings={b1: ["A"], b2: ["B"]},
            outline_section_count=10,
        )
        # Below tokens, but metrics still populated.
        assert "insufficient_tokens" in decision.fail_reasons
        assert decision.total_tokens == 10
        assert decision.distinct_headings == 2


class TestInputMutationSafety:
    """The filter is called once per candidate in a tight loop. It must
    not mutate the inputs (e.g., the caller's `source_provenance` list)
    or subsequent candidates would see corrupted state."""

    def test_evaluate_does_not_mutate_provenance(self) -> None:
        b1 = uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1)
        snapshot = copy.deepcopy(provenance)
        evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 100},
            cited_block_headings={b1: ["S1"]},
            outline_section_count=5,
        )
        assert provenance == snapshot

    def test_evaluate_does_not_mutate_token_counts(self) -> None:
        b1 = uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1)
        token_counts = {b1: 100}
        snapshot = dict(token_counts)
        evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts=token_counts,
            cited_block_headings={b1: ["S1"]},
            outline_section_count=5,
        )
        assert token_counts == snapshot

    def test_evaluate_does_not_mutate_heading_dict(self) -> None:
        b1 = uuid4()
        provenance = _provenance(uuid4(), uuid4(), b1)
        headings = {b1: ["S1"]}
        snapshot = {b1: list(headings[b1])}
        evaluate_candidate(
            source_provenance=provenance,
            cited_block_token_counts={b1: 100},
            cited_block_headings=headings,
            outline_section_count=5,
        )
        assert headings == snapshot


def test_filter_decision_is_frozen() -> None:
    """`FilterDecision` is a `@dataclass(frozen=True)` so callers can't
    accidentally mutate fail_reasons after the fact."""
    decision = FilterDecision(accepted=True, total_tokens=0, distinct_headings=0, fail_reasons=())
    with pytest.raises(FrozenInstanceError):
        decision.accepted = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.fail_reasons = ("a",)  # type: ignore[misc]
