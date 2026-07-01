"""Unit tests for PublishedModuleMerger parsing and prefilter."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest
from platform_service.config import get_settings
from platform_service.services.published_module_merger import (
    PublishedModuleMerger,
    PublishedModuleMergerError,
    _card_fingerprint,
    _existing_card_match_ratio,
    _module_content_similarity,
    _parse_merge_payload,
    _passes_merge_content_gate,
    _prefilter_existing,
    preserve_rich_card_bodies,
)

from tests.localized_helpers import refresher_card

_STRICT_MERGE_ENV = {
    "STAGE_D_PUBLISHED_MERGE_MIN_EXISTING_CARD_MATCH_RATIO": "0.51",
    "STAGE_D_PUBLISHED_MERGE_MODULE_SIMILARITY_THRESHOLD": "0.5",
    "STAGE_D_PUBLISHED_MERGE_CARD_SIMILARITY_THRESHOLD": "0.85",
}


@pytest.fixture
def strict_merge_content_gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in _STRICT_MERGE_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _published(
    module_id: uuid.UUID,
    *,
    title: str,
    cards: list[dict] | None = None,
) -> dict:
    return {
        "module_id": str(module_id),
        "title_localized": {"bn": title, "en": title},
        "cards": cards if cards is not None else [],
    }


def _refresher_card(
    *,
    title: str,
    body: str | dict | list,
    block_id: uuid.UUID | None = None,
) -> dict:
    return refresher_card(
        title=title,
        body=body,
        source_block_ids=[str(block_id or uuid.uuid4())],
    )


def _prosemirror_body(text: str) -> dict:
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _block_list_body(text: str) -> list[dict]:
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def test_card_fingerprint_matches_plain_and_prosemirror_body() -> None:
    body = "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ।"
    block_id = uuid.uuid4()
    plain = _refresher_card(title="কার্ড", body=body, block_id=block_id)
    rich = _refresher_card(title="কার্ড", body=_prosemirror_body(body), block_id=block_id)
    assert _card_fingerprint(plain) == _card_fingerprint(rich)


def test_preserve_rich_card_bodies_keeps_existing_doc() -> None:
    block_id = uuid.uuid4()
    body = "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ।"
    rich_doc = _prosemirror_body(body)
    existing = [
        {
            "card_family_id": str(uuid.uuid4()),
            "title": {"bn": "কার্ড"},
            "body": {"bn": rich_doc},
            "source_block_ids": [str(block_id)],
        }
    ]
    merged = [
        {
            "card_family_id": existing[0]["card_family_id"],
            "title": {"bn": "কার্ড"},
            "body": {"bn": body},
            "source_block_ids": [str(block_id)],
        }
    ]
    out = preserve_rich_card_bodies(merged, existing)
    assert out[0]["body"]["bn"] == rich_doc


def test_preserve_rich_card_bodies_keeps_existing_block_list() -> None:
    block_id = uuid.uuid4()
    body = "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ।"
    rich_blocks = _block_list_body(body)
    existing = [
        {
            "card_family_id": str(uuid.uuid4()),
            "title": {"bn": "কার্ড"},
            "body": {"bn": rich_blocks},
            "source_block_ids": [str(block_id)],
        }
    ]
    merged = [
        {
            "card_family_id": existing[0]["card_family_id"],
            "title": {"bn": "কার্ড"},
            "body": {"bn": body},
            "source_block_ids": [str(block_id)],
        }
    ]
    out = preserve_rich_card_bodies(merged, existing)
    assert out[0]["body"]["bn"] == rich_blocks


def test_prefilter_keeps_top_k_by_title_similarity() -> None:
    target_id = uuid.uuid4()
    modules = [
        _published(uuid.uuid4(), title="Unrelated topic about malaria"),
        _published(target_id, title="Sample Topic for ANC referral"),
        _published(uuid.uuid4(), title="Another unrelated module"),
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
            title="Unrelated malaria prevention",
            cards=[_refresher_card(title="কার্ড", body=shared_body, block_id=block_id)],
        ),
        _published(title_match_id, title="Sample Topic ANC referral", cards=[]),
        _published(uuid.uuid4(), title="Another unrelated module", cards=[]),
    ]
    new_cards = [
        _refresher_card(title="নতুন", body=shared_body, block_id=block_id),
    ]
    out = _prefilter_existing("Sample Topic ANC", modules, new_cards=new_cards, limit=1)
    assert len(out) == 1
    assert out[0]["module_id"] == str(content_match_id)


def test_content_gate_passes_high_overlap() -> None:
    body = "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ এবং রেফারেল পদক্ষেপ।"
    existing = [
        _refresher_card(title="কার্ড এক", body=body),
        _refresher_card(title="কার্ড দুই", body=body + " অতিরিক্ত।"),
    ]
    new = [
        _refresher_card(title="নতুন এক", body=body),
        _refresher_card(title="নতুন দুই", body=body + " অতিরিক্ত।"),
    ]
    ok, detail = _passes_merge_content_gate(existing, new)
    assert ok is True
    assert "content_gate" in detail


def test_content_gate_fails_per_card_majority(strict_merge_content_gate: None) -> None:
    existing = [
        _refresher_card(title="এ", body="এএএ বিষয়বস্তু এক।"),
        _refresher_card(title="বি", body="বিবি বিষয়বস্তু দুই।"),
        _refresher_card(title="সি", body="সিসি বিষয়বস্তু তিন।"),
    ]
    new = [_refresher_card(title="মিল", body="এএএ বিষয়বস্তু এক।")]
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


def test_content_gate_fails_whole_module_similarity(strict_merge_content_gate: None) -> None:
    bid1, bid2 = uuid.uuid4(), uuid.uuid4()
    existing = [
        _refresher_card(title="পুরোনো১", body="xxx", block_id=bid1),
        _refresher_card(title="পুরোনো২", body="yyy", block_id=bid2),
    ]
    new = [
        _refresher_card(title="নতুন১", body="aaa", block_id=bid1),
        _refresher_card(title="নতুন২", body="bbb", block_id=bid2),
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
    existing = [_refresher_card(title="পুরোনো", body="xxx", block_id=block_id)]
    new = [_refresher_card(title="নতুন", body="completely different text", block_id=block_id)]
    ratio = _existing_card_match_ratio(
        existing,
        new,
        card_threshold=get_settings().stage_d_published_merge_card_similarity_threshold,
    )
    assert ratio == 1.0


def test_parse_no_match_returns_original_cards() -> None:
    new_cards = [
        {
            "title": {"bn": "নতুন"},
            "body": {"bn": "বিষয়।"},
            "next_action": {"bn": "পদক্ষেপ।"},
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
        "title": {"bn": "কার্ড"},
        "body": {"bn": "মূল।"},
        "next_action": {"bn": "পদক্ষেপ।"},
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
            existing_modules=[_published(pub_id, title="T")],
            candidate={"proposed_module_type": "refresher"},
            valid_block_ids={block_id},
        )


def test_parse_match_success_when_content_gate_passes() -> None:
    pub_id = uuid.uuid4()
    block_id = uuid.uuid4()
    card = {
        "title": {"bn": "কার্ড"},
        "body": {"bn": "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ এবং রেফারেল।"},
        "next_action": {"bn": "পদক্ষেপ।"},
        "source_block_ids": [str(block_id)],
    }
    result = _parse_merge_payload(
        {
            "matched_module_id": str(pub_id),
            "match_rationale": "same ANC topic",
            "merged_cards": [card],
        },
        new_cards=[card],
        existing_modules=[_published(pub_id, title="T", cards=[card])],
        candidate={"proposed_module_type": "refresher"},
        valid_block_ids={block_id},
    )
    assert result.matched_module_id == pub_id
    assert len(result.merged_cards) == 1


def test_parse_match_rejected_when_content_gate_fails(strict_merge_content_gate: None) -> None:
    pub_id = uuid.uuid4()
    block_id = uuid.uuid4()
    new_card = {
        "title": {"bn": "নতুন"},
        "body": {"bn": "বিষয়।"},
        "next_action": {"bn": "পদক্ষেপ।"},
        "source_block_ids": [str(block_id)],
    }
    unrelated_existing = {
        "title": {"bn": "পুরোনো"},
        "body": {"bn": "সম্পূর্ণ ভিন্ন বিষয়বস্তু ম্যালেরিয়া প্রতিরোধ।"},
        "next_action": {"bn": "পুরনো পদক্ষেপ।"},
        "source_block_ids": [str(uuid.uuid4())],
    }
    merged_card = {
        "title": {"bn": "মার্জ"},
        "body": {"bn": "বিষয়।"},
        "next_action": {"bn": "পদক্ষেপ।"},
        "source_block_ids": [str(block_id)],
    }
    result = _parse_merge_payload(
        {
            "matched_module_id": str(pub_id),
            "match_rationale": "LLM thought match",
            "merged_cards": [merged_card],
        },
        new_cards=[new_card],
        existing_modules=[_published(pub_id, title="T", cards=[unrelated_existing])],
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
    new_cards = [
        {"title": {"bn": "x"}, "body": {"bn": "y"}, "next_action": {"bn": "z"}, "source_block_ids": []}
    ]
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
            "title": {"bn": "নতুন"},
            "body": {"bn": "গর্ভাবস্থায় উচ্চ রক্তচাপ শনাক্তকরণ এবং রেফারেল।"},
            "next_action": {"bn": "পদক্ষেপ।"},
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
            _published(pub_id, title="Sample Topic", cards=list(new_cards)),
        ],
        valid_block_ids={block_id},
    )
    assert result.matched_module_id == pub_id
    client.generate.assert_awaited_once()
