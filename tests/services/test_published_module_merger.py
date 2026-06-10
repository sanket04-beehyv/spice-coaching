"""Unit tests for PublishedModuleMerger parsing and prefilter."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from platform_service.config import get_settings
from platform_service.services.published_module_merger import (
    PublishedModuleMerger,
    PublishedModuleMergerError,
    _existing_card_match_ratio,
    _module_content_similarity,
    _parse_merge_payload,
    _passes_merge_content_gate,
    _prefilter_existing,
)


def _published(
    module_id: uuid.UUID,
    *,
    title_en: str,
    cards: list[dict] | None = None,
) -> dict:
    return {
        "module_id": str(module_id),
        "title_en": title_en,
        "title_bn": title_en,
        "cards": cards if cards is not None else [],
    }


def _refresher_card(
    *,
    title_bn: str,
    body_bn: str,
    block_id: uuid.UUID | None = None,
) -> dict:
    return {
        "title_bn": title_bn,
        "body_bn": body_bn,
        "next_action_bn": "পদক্ষেপ।",
        "source_block_ids": [str(block_id or uuid.uuid4())],
    }


def test_prefilter_keeps_top_k_by_title_similarity() -> None:
    target_id = uuid.uuid4()
    modules = [
        _published(uuid.uuid4(), title_en="Unrelated topic about malaria"),
        _published(target_id, title_en="Sample Topic for ANC referral"),
        _published(uuid.uuid4(), title_en="Another unrelated module"),
    ]
    out = _prefilter_existing("Sample Topic ANC", modules, new_cards=[], limit=2)
    ids = {m["module_id"] for m in out}
    assert str(target_id) in ids
    assert len(out) == 2


def test_prefilter_ranks_by_card_content_over_weak_title() -> None:
    block_id = uuid.uuid4()
    shared_body = "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ এবং তাত্ক্ষণিক রেফারেলের সঠিক পদক্ষেপ অনুসরণ করুন।"
    content_match_id = uuid.uuid4()
    title_match_id = uuid.uuid4()
    modules = [
        _published(
            content_match_id,
            title_en="Unrelated malaria prevention",
            cards=[_refresher_card(title_bn="কার্ড", body_bn=shared_body, block_id=block_id)],
        ),
        _published(title_match_id, title_en="Sample Topic ANC referral", cards=[]),
        _published(uuid.uuid4(), title_en="Another unrelated module", cards=[]),
    ]
    new_cards = [
        _refresher_card(title_bn="নতুন", body_bn=shared_body, block_id=block_id),
    ]
    out = _prefilter_existing("Sample Topic ANC", modules, new_cards=new_cards, limit=1)
    assert len(out) == 1
    assert out[0]["module_id"] == str(content_match_id)


def test_content_gate_passes_high_overlap() -> None:
    body = "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ এবং রেফারেল পদক্ষেপ।"
    existing = [
        _refresher_card(title_bn="কার্ড এক", body_bn=body),
        _refresher_card(title_bn="কার্ড দুই", body_bn=body + " অতিরিক্ত।"),
    ]
    new = [
        _refresher_card(title_bn="নতুন এক", body_bn=body),
        _refresher_card(title_bn="নতুন দুই", body_bn=body + " অতিরিক্ত।"),
    ]
    ok, detail = _passes_merge_content_gate(existing, new)
    assert ok is True
    assert "content_gate" in detail


def test_content_gate_fails_per_card_majority() -> None:
    existing = [
        _refresher_card(title_bn="এ", body_bn="এএএ বিষয়বস্তু এক।"),
        _refresher_card(title_bn="বি", body_bn="বিবি বিষয়বস্তু দুই।"),
        _refresher_card(title_bn="সি", body_bn="সিসি বিষয়বস্তু তিন।"),
    ]
    new = [_refresher_card(title_bn="মিল", body_bn="এএএ বিষয়বস্তু এক।")]
    ok, detail = _passes_merge_content_gate(existing, new)
    assert ok is False
    assert "content_gate" in detail
    assert (
        _existing_card_match_ratio(
            existing,
            new,
            card_threshold=get_settings().stage_d_published_merge_card_similarity_threshold,
        )
        < get_settings().stage_d_published_merge_min_existing_card_match_ratio
    )


def test_content_gate_fails_whole_module_similarity() -> None:
    bid1, bid2 = uuid.uuid4(), uuid.uuid4()
    existing = [
        _refresher_card(title_bn="পুরোনো১", body_bn="xxx", block_id=bid1),
        _refresher_card(title_bn="পুরোনো২", body_bn="yyy", block_id=bid2),
    ]
    new = [
        _refresher_card(title_bn="নতুন১", body_bn="aaa", block_id=bid1),
        _refresher_card(title_bn="নতুন২", body_bn="bbb", block_id=bid2),
    ]
    assert (
        _existing_card_match_ratio(
            existing,
            new,
            card_threshold=get_settings().stage_d_published_merge_card_similarity_threshold,
        )
        == 1.0
    )
    assert _module_content_similarity(existing, new) < (
        get_settings().stage_d_published_merge_module_similarity_threshold
    )
    ok, _ = _passes_merge_content_gate(existing, new)
    assert ok is False


def test_block_id_counts_toward_card_match() -> None:
    block_id = uuid.uuid4()
    existing = [_refresher_card(title_bn="পুরোনো", body_bn="xxx", block_id=block_id)]
    new = [_refresher_card(title_bn="নতুন", body_bn="completely different text", block_id=block_id)]
    ratio = _existing_card_match_ratio(
        existing,
        new,
        card_threshold=get_settings().stage_d_published_merge_card_similarity_threshold,
    )
    assert ratio == 1.0


def test_parse_no_match_returns_original_cards() -> None:
    new_cards = [
        {
            "title_bn": "নতুন",
            "body_bn": "বিষয়।",
            "next_action_bn": "পদক্ষেপ।",
            "source_block_ids": [str(uuid.uuid4())],
        }
    ]
    block_id = uuid.UUID(new_cards[0]["source_block_ids"][0])
    result = _parse_merge_payload(
        {
            "matched_module_id": None,
            "match_rationale": "different topics",
            "merged_cards": new_cards,
        },
        new_cards=new_cards,
        existing_modules=[],
        candidate={"proposed_module_type": "refresher"},
        valid_block_ids={block_id},
    )
    assert result.matched_module_id is None
    assert result.merged_cards == new_cards


def test_parse_match_requires_valid_module_id() -> None:
    pub_id = uuid.uuid4()
    block_id = uuid.uuid4()
    card = {
        "title_bn": "কার্ড",
        "body_bn": "মূল।",
        "next_action_bn": "পদক্ষেপ।",
        "source_block_ids": [str(block_id)],
    }
    with pytest.raises(PublishedModuleMergerError, match="existing-module"):
        _parse_merge_payload(
            {
                "matched_module_id": str(uuid.uuid4()),
                "match_rationale": "x",
                "merged_cards": [card],
            },
            new_cards=[card],
            existing_modules=[_published(pub_id, title_en="T")],
            candidate={"proposed_module_type": "refresher"},
            valid_block_ids={block_id},
        )


def test_parse_match_success_when_content_gate_passes() -> None:
    pub_id = uuid.uuid4()
    block_id = uuid.uuid4()
    card = {
        "title_bn": "কার্ড",
        "body_bn": "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ এবং রেফারেল।",
        "next_action_bn": "পদক্ষেপ।",
        "source_block_ids": [str(block_id)],
    }
    result = _parse_merge_payload(
        {
            "matched_module_id": str(pub_id),
            "match_rationale": "same ANC topic",
            "merged_cards": [card],
        },
        new_cards=[card],
        existing_modules=[_published(pub_id, title_en="T", cards=[card])],
        candidate={"proposed_module_type": "refresher"},
        valid_block_ids={block_id},
    )
    assert result.matched_module_id == pub_id
    assert len(result.merged_cards) == 1


def test_parse_match_rejected_when_content_gate_fails() -> None:
    pub_id = uuid.uuid4()
    block_id = uuid.uuid4()
    new_card = {
        "title_bn": "নতুন",
        "body_bn": "বিষয়।",
        "next_action_bn": "পদক্ষেপ।",
        "source_block_ids": [str(block_id)],
    }
    unrelated_existing = {
        "title_bn": "পুরোনো",
        "body_bn": "সম্পূর্ণ ভিন্ন বিষয়বস্তু ম্যালেরিয়া প্রতিরোধ।",
        "next_action_bn": "পুরনো পদক্ষেপ।",
        "source_block_ids": [str(uuid.uuid4())],
    }
    merged_card = {
        "title_bn": "মার্জ",
        "body_bn": "বিষয়।",
        "next_action_bn": "পদক্ষেপ।",
        "source_block_ids": [str(block_id)],
    }
    result = _parse_merge_payload(
        {
            "matched_module_id": str(pub_id),
            "match_rationale": "LLM thought match",
            "merged_cards": [merged_card],
        },
        new_cards=[new_card],
        existing_modules=[_published(pub_id, title_en="T", cards=[unrelated_existing])],
        candidate={"proposed_module_type": "refresher"},
        valid_block_ids={block_id},
    )
    assert result.matched_module_id is None
    assert result.merged_cards == [new_card]
    assert result.match_rationale is not None
    assert "content_gate" in result.match_rationale


@pytest.mark.asyncio
async def test_merge_empty_published_returns_new_cards() -> None:
    merger = PublishedModuleMerger(client=MagicMock())
    new_cards = [{"title_bn": "x", "body_bn": "y", "next_action_bn": "z", "source_block_ids": []}]
    result = await merger.merge(
        candidate={"proposed_title": "T", "proposed_module_type": "refresher"},
        new_cards=new_cards,
        published_modules=[],
        valid_block_ids=set(),
    )
    assert result.matched_module_id is None
    assert result.merged_cards == new_cards


@pytest.mark.asyncio
async def test_merge_calls_ai_runtime_and_passes_content_gate() -> None:
    block_id = uuid.uuid4()
    pub_id = uuid.uuid4()
    new_cards = [
        {
            "title_bn": "নতুন",
            "body_bn": "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ এবং রেফারেল।",
            "next_action_bn": "পদক্ষেপ।",
            "source_block_ids": [str(block_id)],
        }
    ]
    client = MagicMock()
    client.generate = AsyncMock(
        return_value=MagicMock(
            error=None,
            parsed_json={
                "matched_module_id": str(pub_id),
                "match_rationale": "match",
                "merged_cards": new_cards,
            },
            raw_text="",
        )
    )
    merger = PublishedModuleMerger(client=client)
    result = await merger.merge(
        candidate={"proposed_title": "Sample", "proposed_module_type": "refresher"},
        new_cards=new_cards,
        published_modules=[
            _published(pub_id, title_en="Sample Topic", cards=list(new_cards)),
        ],
        valid_block_ids={block_id},
    )
    assert result.matched_module_id == pub_id
    client.generate.assert_awaited_once()
