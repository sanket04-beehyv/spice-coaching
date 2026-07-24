"""W-5 — card_drafter unit tests with mocked AIRuntimeClient."""

import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceRequest, InferenceResponse
from platform_service.services.card_drafter import (
    CardDrafter,
    CardDrafterError,
)
from platform_service.services.prompt_variables.card_drafter_variables import build_card_drafter_variables
from platform_service.services.prompts.card_drafter_prompt import _SYSTEM_BASE

pytestmark = pytest.mark.usefixtures("mock_prompt_templates")


def _resp(parsed_json: Any = None, raw_text: str = "", error: str | None = None) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-1",
        generation_type=GenerationType.CARD_DRAFTING,
        provider="google",
        model="gemini-2.5-flash",
        max_tokens=8192,
        temperature=0.2,
        raw_text=raw_text,
        parsed_json=parsed_json,
        latency_ms=200,
        error=error,
    )


def _refresher_card(block_ids: list[str], *, title: str = "Card 1") -> dict[str, Any]:
    return {
        "card_order": 1,
        "title": {"bn": title},
        "body": {"bn": "বাংলা বডি কন্টেন্ট"},
        "source_block_ids": block_ids,
    }


def _content_update_card(block_ids: list[str]) -> dict[str, Any]:
    return {
        "card_order": 1,
        "title": {"bn": "Title"},
        "body": {"bn": ""},
        "previous_practice": {"bn": "আগে"},
        "current_practice": {"bn": "এখন"},
        "rationale_for_change": {"bn": "কারণ"},
        "source_block_ids": block_ids,
    }


def _candidate(module_type: str = "refresher") -> dict[str, Any]:
    return {
        "proposed_title": "Module",
        "behavioural_gap_code": "incorrect_referral_destination",
        "scope_summary": "Topic scope.",
        "proposed_module_type": module_type,
        "estimated_card_count": 5,
    }


class TestCardDrafterHappyPath:
    @pytest.mark.asyncio
    async def test_returns_normalised_cards(self) -> None:
        b1, b2 = str(uuid4()), str(uuid4())
        cards_payload = {
            "cards": [
                _refresher_card([b1], title="Card 1"),
                _refresher_card([b1, b2], title="Card 2"),
                _refresher_card([b2], title="Card 3"),
            ]
        }
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(parsed_json=cards_payload))
        drafter = CardDrafter(client=client)

        result = await drafter.draft(
            candidate=_candidate(),
            cited_blocks=[],
            valid_block_ids={UUID(b1), UUID(b2)},
        )
        assert result.insufficient_reason is None
        assert len(result.cards) == 3
        assert all("card_family_id" in c for c in result.cards)

    @pytest.mark.asyncio
    async def test_uses_card_drafting_generation_type(self) -> None:
        b1 = str(uuid4())
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(parsed_json={"cards": [_refresher_card([b1])] * 3}))
        drafter = CardDrafter(client=client)
        await drafter.draft(
            candidate=_candidate(),
            cited_blocks=[],
            valid_block_ids={UUID(b1)},
        )
        sent: InferenceRequest = client.generate.call_args.args[0]
        assert sent.generation_type == GenerationType.CARD_DRAFTING


class TestCardDrafterValidation:
    @pytest.mark.asyncio
    async def test_missing_body_bn_for_refresher_rejected(self) -> None:
        b1 = str(uuid4())
        bad = _refresher_card([b1])
        bad["body"]["bn"] = ""
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=_resp(parsed_json={"cards": [bad, _refresher_card([b1]), _refresher_card([b1])]})
        )
        drafter = CardDrafter(client=client)
        result = await drafter.draft(
            candidate=_candidate(),
            cited_blocks=[],
            valid_block_ids={UUID(b1)},
        )
        # Per the architecture reset: bad card is dropped, surviving cards
        # are emitted (no count-bounds rejection). The dashboard surfaces
        # low-card modules via quality_flags instead of failing them.
        assert result.insufficient_reason is None
        assert len(result.cards) == 2

    @pytest.mark.asyncio
    async def test_missing_body_bn_for_initial_training_rejected(self) -> None:
        """B3 review finding: the elif-chain in `_normalise_card` used to
        skip `initial_training`, so cards with empty body_bn slipped
        through and got published unrenderable."""
        b1 = str(uuid4())
        bad = _refresher_card([b1])  # same shape; module_type drives validation
        bad["body"]["bn"] = ""
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=_resp(parsed_json={"cards": [bad, _refresher_card([b1]), _refresher_card([b1])]})
        )
        drafter = CardDrafter(client=client)
        result = await drafter.draft(
            candidate=_candidate("initial_training"),
            cited_blocks=[],
            valid_block_ids={UUID(b1)},
        )
        # Bad initial_training card dropped, 2 valid cards survive.
        assert result.insufficient_reason is None
        assert len(result.cards) == 2

    @pytest.mark.asyncio
    async def test_card_family_id_is_always_server_issued_uuid(self) -> None:
        """B4 review finding: the LLM's free-form `card_family_id` was
        accepted as-is. Now we always overwrite with a fresh UUID so a
        hallucinated non-UUID can't corrupt module_quiz_question joins."""
        b1 = str(uuid4())
        c = _refresher_card([b1])
        c["card_family_id"] = "not-a-uuid"  # hallucinated
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=_resp(parsed_json={"cards": [c, _refresher_card([b1]), _refresher_card([b1])]})
        )
        drafter = CardDrafter(client=client)
        result = await drafter.draft(
            candidate=_candidate(),
            cited_blocks=[],
            valid_block_ids={UUID(b1)},
        )
        assert len(result.cards) == 3
        for card in result.cards:
            # Every card has a valid UUID stamped — even the one that came
            # in with a hallucinated string.
            UUID(card["card_family_id"])

    @pytest.mark.asyncio
    async def test_content_update_missing_required_field_rejected(self) -> None:
        b1 = str(uuid4())
        bad = _content_update_card([b1])
        del bad["previous_practice"]
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=_resp(
                parsed_json={
                    "cards": [
                        bad,
                        _content_update_card([b1]),
                        _content_update_card([b1]),
                    ]
                }
            )
        )
        drafter = CardDrafter(client=client)
        result = await drafter.draft(
            candidate=_candidate("content_update"),
            cited_blocks=[],
            valid_block_ids={UUID(b1)},
        )
        # Bad card dropped, 2 valid content_update cards survive.
        assert result.insufficient_reason is None
        assert len(result.cards) == 2

    @pytest.mark.asyncio
    async def test_invalid_source_block_id_dropped(self) -> None:
        b_real = str(uuid4())
        b_fake = str(uuid4())
        bad = _refresher_card([b_fake])
        # No valid blocks → card rejected
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(parsed_json={"cards": [bad, bad, bad]}))
        drafter = CardDrafter(client=client)
        result = await drafter.draft(
            candidate=_candidate(),
            cited_blocks=[],
            valid_block_ids={UUID(b_real)},  # b_fake not present
        )
        # ALL cards dropped (none had valid block IDs) → no_actionable_content.
        assert result.insufficient_reason == "no_actionable_content"
        assert result.cards == []

    @pytest.mark.asyncio
    async def test_caps_at_max_cards(self) -> None:
        b1 = str(uuid4())
        cards = [_refresher_card([b1], title=f"Card {i}") for i in range(20)]
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(parsed_json={"cards": cards}))
        drafter = CardDrafter(client=client)
        result = await drafter.draft(
            candidate=_candidate(),
            cited_blocks=[],
            valid_block_ids={UUID(b1)},
        )
        # card_max_count default is 7
        assert len(result.cards) == 7

    @pytest.mark.asyncio
    async def test_figure_ref_invalid_block_set_to_null(self) -> None:
        b_real = str(uuid4())
        b_fake = str(uuid4())
        card = _refresher_card([b_real])
        card["figure_ref_block_id"] = b_fake  # invalid
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=_resp(
                parsed_json={"cards": [card, _refresher_card([b_real]), _refresher_card([b_real])]}
            )
        )
        drafter = CardDrafter(client=client)
        result = await drafter.draft(
            candidate=_candidate(),
            cited_blocks=[],
            valid_block_ids={UUID(b_real)},
        )
        assert result.cards[0].get("figure_ref_block_id") is None


class TestCardDrafterRefusalPath:
    @pytest.mark.asyncio
    async def test_llm_refusal_path(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=_resp(parsed_json={"insufficient_for_drafting": "no_actionable_content"})
        )
        drafter = CardDrafter(client=client)
        result = await drafter.draft(
            candidate=_candidate(),
            cited_blocks=[],
            valid_block_ids=set(),
        )
        assert result.insufficient_reason == "no_actionable_content"
        assert result.cards == []


class TestCardDrafterErrorPaths:
    @pytest.mark.asyncio
    async def test_runtime_error_raises(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(error="boom"))
        drafter = CardDrafter(client=client)
        with pytest.raises(CardDrafterError, match="boom"):
            await drafter.draft(
                candidate=_candidate(),
                cited_blocks=[],
                valid_block_ids=set(),
            )

    @pytest.mark.asyncio
    async def test_invalid_json_raises(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(raw_text="not json{{{"))
        drafter = CardDrafter(client=client)
        with pytest.raises(CardDrafterError, match="not valid JSON"):
            await drafter.draft(
                candidate=_candidate(),
                cited_blocks=[],
                valid_block_ids=set(),
            )

    @pytest.mark.asyncio
    async def test_non_object_payload_raises(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(raw_text=json.dumps([])))
        drafter = CardDrafter(client=client)
        with pytest.raises(CardDrafterError, match="must be a JSON object"):
            await drafter.draft(
                candidate=_candidate(),
                cited_blocks=[],
                valid_block_ids=set(),
            )


# ─── Multi-source coverage (v2 prompt rule) ─────────────────────────────────


class TestMultiSourceCoverage:
    """v2 of the drafter prompt added a cross-source coverage rule for
    fused candidates whose cited_blocks span multiple source_document_ids.
    Surfaced on the BRAC + UHIS Family Planning fused-candidate
    experiment (2026-05-09): without this, the LLM produced 5 cards all
    citing BRAC blocks, ignoring 10 UHIS workflow blocks. The prompt
    must:
      - Tag every block with a short `source=d{n}` label
      - Surface a multi-source notice when blocks span ≥2 sources
      - Carry a hard rule in the system prompt requiring per-source
        coverage when the multi-source case applies
    """

    def test_template_has_cross_source_rule_in_db_template(self) -> None:
        lowered = _SYSTEM_BASE.lower()
        assert "cross-source coverage" in lowered

    def test_system_prompt_has_symbol_verbalization_rules(self) -> None:
        from platform_service.services.prompts.symbol_verbalization import SYMBOL_VERBALIZATION_RULES

        variables = build_card_drafter_variables(
            module_type="initial_training",
            card_min_count=3,
            card_max_count=7,
            candidate=_candidate(),
            cited_blocks=[],
        )
        rules = variables["symbol_verbalization_rules"]
        assert SYMBOL_VERBALIZATION_RULES.strip() in rules
        assert "20 থেকে 30" in rules
        assert "blood-pressure pair" in rules.lower()
        assert "dose fraction" in rules.lower()
        assert "render mathematical symbols" in rules.lower()
        assert "spoken language" in rules.lower()
        assert "resolves conflicts with" in rules.lower()
        assert "terminology only" in _SYSTEM_BASE.lower()

    def test_system_prompt_has_cross_source_rule(self) -> None:
        lowered = _SYSTEM_BASE.lower()
        assert "cross-source coverage" in lowered
        # The rule must explicitly require per-source citation when blocks
        # span multiple sources.
        assert "every source label" in lowered or "each source label" in lowered
        # And must explain what happens when only one source is present
        # (no-op, single-source behaviour preserved).
        assert "single source" in lowered or "no-op" in lowered

    def test_human_message_tags_blocks_with_source_labels(self) -> None:
        """Each block must carry `source=d{n}` so the LLM can plan card
        coverage by source. First-seen ordering keeps labels stable
        within one render call."""
        sd_a, sd_b = str(uuid4()), str(uuid4())
        blocks = [
            {
                "content_block_id": "b1",
                "source_document_id": sd_a,
                "block_type": "paragraph",
                "content_text": "BRAC clinical content",
                "content_language": "bn",
            },
            {
                "content_block_id": "b2",
                "source_document_id": sd_b,
                "block_type": "paragraph",
                "content_text": "UHIS workflow content",
                "content_language": "en",
            },
        ]
        variables = build_card_drafter_variables(
            module_type="refresher",
            card_min_count=3,
            card_max_count=7,
            candidate=_candidate(),
            cited_blocks=blocks,
        )
        msg = variables["head_json"] + variables["cited_blocks_body"]
        assert "source=d1" in msg
        assert "source=d2" in msg
        # Multi-source notice surfaces explicitly so the LLM can plan.
        assert "multi-source candidate" in msg

    def test_human_message_single_source_no_multi_notice(self) -> None:
        """When all blocks share one source, the multi-source notice
        must not appear. Source label still tags blocks (benignly) so
        the prompt body shape stays uniform."""
        sd = str(uuid4())
        blocks = [
            {
                "content_block_id": f"b{i}",
                "source_document_id": sd,
                "block_type": "paragraph",
                "content_text": f"text {i}",
                "content_language": "bn",
            }
            for i in range(3)
        ]
        variables = build_card_drafter_variables(
            module_type="refresher",
            card_min_count=3,
            card_max_count=7,
            candidate=_candidate(),
            cited_blocks=blocks,
        )
        msg = variables["head_json"] + variables["cited_blocks_body"]
        assert "source=d1" in msg
        assert "source=d2" not in msg
        assert "multi-source candidate" not in msg

    def test_human_message_per_source_counts_visible(self) -> None:
        """The multi-source notice must include per-label block counts so
        the LLM can see which source is under-represented (e.g. 47 vs 10
        on the FP experiment) and plan to cite the smaller pile anyway."""
        sd_a, sd_b = str(uuid4()), str(uuid4())
        blocks = [
            {
                "content_block_id": f"a{i}",
                "source_document_id": sd_a,
                "block_type": "paragraph",
                "content_text": "x",
                "content_language": "bn",
            }
            for i in range(5)
        ] + [
            {
                "content_block_id": "b1",
                "source_document_id": sd_b,
                "block_type": "paragraph",
                "content_text": "y",
                "content_language": "en",
            }
        ]
        variables = build_card_drafter_variables(
            module_type="refresher",
            card_min_count=3,
            card_max_count=7,
            candidate=_candidate(),
            cited_blocks=blocks,
        )
        msg = variables["head_json"] + variables["cited_blocks_body"]
        # Counts should show 5 in d1 and 1 in d2 — the LLM needs to see
        # the imbalance to apply the per-source coverage rule correctly.
        assert "'d1': 5" in msg
        assert "'d2': 1" in msg
