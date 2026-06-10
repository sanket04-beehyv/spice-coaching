"""Stage 2b cross-source fuser tests.

The fuser operates on candidate metadata only — no raw corpus, no
provenance lookup. Tests mock the AI runtime client and verify the
validate-and-partition logic plus the structural guarantees of the
output (group spans ≥2 distinct sources, candidate appears in exactly
one of {group, unfused}, etc.).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceRequest, InferenceResponse, TokenUsage
from platform_service.services.cross_source_fuser import (
    CrossSourceFuser,
    CrossSourceFuserError,
)


def _candidate(
    *,
    cid: UUID | None = None,
    source_doc_id: UUID | None = None,
    title: str = "Sample",
    scope: str = "Sample scope.",
) -> dict[str, Any]:
    return {
        "id": cid or uuid4(),
        "source_document_id": source_doc_id or uuid4(),
        "proposed_title": title,
        "scope_summary": scope,
    }


def _mock_response(
    parsed_json: Any = None, *, raw_text: str = "", error: str | None = None
) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-1",
        generation_type=GenerationType.CROSS_SOURCE_FUSION,
        provider="google",
        model="gemini-2.5-flash",
        raw_text=raw_text,
        parsed_json=parsed_json,
        latency_ms=200,
        token_usage=TokenUsage(input=300, output=200),
        error=error,
    )


# ─── Pre-conditions: nothing-to-fuse short-circuits ─────────────────────────


class TestNothingToFuse:
    @pytest.mark.asyncio
    async def test_empty_input_returns_empty_result(self) -> None:
        client = MagicMock()
        client.generate = AsyncMock()
        fuser = CrossSourceFuser(client=client)
        result = await fuser.fuse([])
        assert result.fusion_groups == []
        assert result.unfused_ids == []
        client.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_candidate_returns_unfused_no_llm_call(self) -> None:
        client = MagicMock()
        client.generate = AsyncMock()
        fuser = CrossSourceFuser(client=client)
        c = _candidate()
        result = await fuser.fuse([c])
        assert result.fusion_groups == []
        assert result.unfused_ids == [UUID(str(c["id"]))]
        client.generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_candidates_same_source_skips_llm(self) -> None:
        """When all input candidates share one source_document_id there
        is no fusion to do across sources — short-circuit before paying
        for an LLM call."""
        client = MagicMock()
        client.generate = AsyncMock()
        fuser = CrossSourceFuser(client=client)
        same_source = uuid4()
        cands = [_candidate(source_doc_id=same_source) for _ in range(5)]
        result = await fuser.fuse(cands)
        assert result.fusion_groups == []
        assert len(result.unfused_ids) == 5
        client.generate.assert_not_awaited()


# ─── Happy path: well-formed fusion group ───────────────────────────────────


class TestFusionHappyPath:
    @pytest.mark.asyncio
    async def test_one_group_two_sources_passes(self) -> None:
        brac_doc, uhis_doc = uuid4(), uuid4()
        c_brac = _candidate(source_doc_id=brac_doc, title="ANC clinical")
        c_uhis = _candidate(source_doc_id=uhis_doc, title="ANC App workflow")
        c_solo = _candidate(source_doc_id=brac_doc, title="Diabetes")

        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                {
                    "fusion_groups": [
                        {
                            "candidate_ids": [str(c_brac["id"]), str(c_uhis["id"])],
                            "merged_title": "ANC visit: clinical + UHIS workflow",
                            "merged_scope_summary": "Combined ANC unit.",
                            "pairing_rationale": "Same CHW activity, two angles.",
                        }
                    ]
                }
            )
        )
        fuser = CrossSourceFuser(client=client)
        result = await fuser.fuse([c_brac, c_uhis, c_solo])

        assert len(result.fusion_groups) == 1
        g = result.fusion_groups[0]
        assert set(g.constituent_ids) == {UUID(str(c_brac["id"])), UUID(str(c_uhis["id"]))}
        assert g.merged_title == "ANC visit: clinical + UHIS workflow"
        # The unfused list is the BRAC-Diabetes candidate.
        assert result.unfused_ids == [UUID(str(c_solo["id"]))]

    @pytest.mark.asyncio
    async def test_inference_request_uses_cross_source_fusion_type(self) -> None:
        """Wire-level smoke: the dispatched InferenceRequest carries the
        correct GenerationType so ai-runtime metrics + caching key on
        the right channel."""
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response({"fusion_groups": []}))
        fuser = CrossSourceFuser(client=client)
        await fuser.fuse([_candidate(), _candidate()])
        sent: InferenceRequest = client.generate.call_args.args[0]
        assert sent.generation_type == GenerationType.CROSS_SOURCE_FUSION
        assert sent.constraints.output_format == "json"


# ─── Validation: hallucinations dropped, structural rules enforced ──────────


class TestFusionValidation:
    @pytest.mark.asyncio
    async def test_hallucinated_id_dropped_from_group(self) -> None:
        """LLM emits a candidate_id not in the input set → that ID is
        dropped. If the group still has ≥2 valid constituents from ≥2
        sources, it survives."""
        brac_doc, uhis_doc = uuid4(), uuid4()
        c_brac = _candidate(source_doc_id=brac_doc)
        c_uhis = _candidate(source_doc_id=uhis_doc)
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                {
                    "fusion_groups": [
                        {
                            "candidate_ids": [
                                str(c_brac["id"]),
                                str(c_uhis["id"]),
                                str(uuid4()),  # hallucinated
                            ],
                            "merged_title": "T",
                            "merged_scope_summary": "S",
                            "pairing_rationale": "R",
                        }
                    ]
                }
            )
        )
        fuser = CrossSourceFuser(client=client)
        result = await fuser.fuse([c_brac, c_uhis])
        assert len(result.fusion_groups) == 1
        # Only the real IDs survive.
        assert set(result.fusion_groups[0].constituent_ids) == {
            UUID(str(c_brac["id"])),
            UUID(str(c_uhis["id"])),
        }

    @pytest.mark.asyncio
    async def test_group_with_one_valid_id_after_drops_rejected(self) -> None:
        """If hallucination drops bring a group below 2 valid IDs, the
        whole group is rejected and its valid ID falls into unfused."""
        brac_doc, uhis_doc = uuid4(), uuid4()
        c_brac = _candidate(source_doc_id=brac_doc)
        c_uhis = _candidate(source_doc_id=uhis_doc)
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                {
                    "fusion_groups": [
                        {
                            "candidate_ids": [
                                str(c_brac["id"]),
                                str(uuid4()),  # hallucinated
                                str(uuid4()),  # hallucinated
                            ],
                            "merged_title": "T",
                            "merged_scope_summary": "S",
                            "pairing_rationale": "R",
                        }
                    ]
                }
            )
        )
        fuser = CrossSourceFuser(client=client)
        result = await fuser.fuse([c_brac, c_uhis])
        assert result.fusion_groups == []
        assert set(result.unfused_ids) == {
            UUID(str(c_brac["id"])),
            UUID(str(c_uhis["id"])),
        }

    @pytest.mark.asyncio
    async def test_group_all_same_source_rejected(self) -> None:
        """LLM emits a 'group' of two candidates from the same source —
        invalid by definition; rejected, both treated as unfused."""
        same_doc = uuid4()
        other_doc = uuid4()
        c1 = _candidate(source_doc_id=same_doc)
        c2 = _candidate(source_doc_id=same_doc)
        c3 = _candidate(source_doc_id=other_doc)
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                {
                    "fusion_groups": [
                        {
                            "candidate_ids": [str(c1["id"]), str(c2["id"])],
                            "merged_title": "T",
                            "merged_scope_summary": "S",
                            "pairing_rationale": "R",
                        }
                    ]
                }
            )
        )
        fuser = CrossSourceFuser(client=client)
        result = await fuser.fuse([c1, c2, c3])
        assert result.fusion_groups == []
        assert len(result.unfused_ids) == 3

    @pytest.mark.asyncio
    async def test_candidate_in_two_groups_first_wins(self) -> None:
        """Same id in two groups → first group keeps it, second group
        loses it. If second group falls below 2 valid → it's rejected."""
        d1, d2, d3 = uuid4(), uuid4(), uuid4()
        c1 = _candidate(source_doc_id=d1)
        c2 = _candidate(source_doc_id=d2)
        c3 = _candidate(source_doc_id=d3)
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                {
                    "fusion_groups": [
                        {
                            "candidate_ids": [str(c1["id"]), str(c2["id"])],
                            "merged_title": "G1",
                            "merged_scope_summary": "S1",
                            "pairing_rationale": "R1",
                        },
                        {
                            # c1 is repeated; only c3 will remain → group dies
                            "candidate_ids": [str(c1["id"]), str(c3["id"])],
                            "merged_title": "G2",
                            "merged_scope_summary": "S2",
                            "pairing_rationale": "R2",
                        },
                    ]
                }
            )
        )
        fuser = CrossSourceFuser(client=client)
        result = await fuser.fuse([c1, c2, c3])
        assert len(result.fusion_groups) == 1
        assert result.fusion_groups[0].merged_title == "G1"
        # c3 was the lone survivor of the rejected G2 → ends up unfused.
        assert result.unfused_ids == [UUID(str(c3["id"]))]


# ─── Failure handling ───────────────────────────────────────────────────────


class TestFuserFailures:
    @pytest.mark.asyncio
    async def test_ai_runtime_error_raises(self) -> None:
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response(error="429 RESOURCE_EXHAUSTED"))
        fuser = CrossSourceFuser(client=client)
        with pytest.raises(CrossSourceFuserError, match="429"):
            await fuser.fuse([_candidate(), _candidate()])

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self) -> None:
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response(None, raw_text="not json {{"))
        fuser = CrossSourceFuser(client=client)
        with pytest.raises(CrossSourceFuserError, match="not valid JSON"):
            await fuser.fuse([_candidate(), _candidate()])

    @pytest.mark.asyncio
    async def test_empty_fusion_groups_returns_all_unfused(self) -> None:
        """LLM legitimately concludes no fusions are warranted — every
        input candidate ends up in unfused."""
        client = MagicMock()
        client.generate = AsyncMock(return_value=_mock_response({"fusion_groups": []}))
        fuser = CrossSourceFuser(client=client)
        c1, c2 = _candidate(), _candidate()
        result = await fuser.fuse([c1, c2])
        assert result.fusion_groups == []
        assert set(result.unfused_ids) == {UUID(str(c1["id"])), UUID(str(c2["id"]))}


# ─── Partition invariant ────────────────────────────────────────────────────


class TestPartitionInvariant:
    @pytest.mark.asyncio
    async def test_every_input_id_appears_exactly_once_in_output(self) -> None:
        """Hard invariant: union of fusion group constituents and unfused
        ids equals the input id set; intersection is empty. Caller relies
        on this to safely replace constituents with fused candidates
        without losing or double-counting any."""
        d1, d2 = uuid4(), uuid4()
        cands = [
            _candidate(source_doc_id=d1, title="A"),
            _candidate(source_doc_id=d1, title="B"),
            _candidate(source_doc_id=d2, title="C"),
            _candidate(source_doc_id=d2, title="D"),
        ]
        # LLM fuses A+C; B and D stay alone.
        client = MagicMock()
        client.generate = AsyncMock(
            return_value=_mock_response(
                {
                    "fusion_groups": [
                        {
                            "candidate_ids": [str(cands[0]["id"]), str(cands[2]["id"])],
                            "merged_title": "AC",
                            "merged_scope_summary": "S",
                            "pairing_rationale": "R",
                        }
                    ]
                }
            )
        )
        fuser = CrossSourceFuser(client=client)
        result = await fuser.fuse(cands)
        all_input_ids = {UUID(str(c["id"])) for c in cands}
        all_output_ids: set[UUID] = set()
        for g in result.fusion_groups:
            all_output_ids.update(g.constituent_ids)
        all_output_ids.update(result.unfused_ids)
        assert all_output_ids == all_input_ids
        assert sum(len(g.constituent_ids) for g in result.fusion_groups) + len(result.unfused_ids) == len(
            cands
        )
