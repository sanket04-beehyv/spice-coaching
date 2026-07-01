"""Direct tests for card search metadata normalization and generator."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, TokenUsage
from platform_service.db.models.module import Module
from platform_service.services.card_search_metadata_generator import (
    CardSearchMetadataGenerator,
    card_metadata_has_searchable_content,
    normalize_card_search_metadata,
    parse_batch_card_search_metadata,
)
from platform_service.services.prompts.card_search_metadata_prompt import (
    CARD_SEARCH_METADATA_TEMPLATE_VERSION,
)


def _sample_payload() -> dict:
    return {
        "schema_version": 1,
        "retrieval_hints": {
            "bn": ["১৪ দিনের বেশি কাশি"],
            "en": ["child cough more than 14 days"],
        },
        "keywords": {"bn": ["কাশি"], "en": ["ARI", "cough"]},
        "synonyms_en": {"ARI": "acute respiratory infection"},
        "questions": {
            "bn": ["কাশি হলে কখন রেফার করব?"],
            "en": ["When should I refer a child with cough?"],
        },
    }


def _batch_payload(*, indices: list[int]) -> dict:
    return {
        "schema_version": 1,
        "cards": [
            {"card_index": idx, **_sample_payload(), "keywords": {"bn": [f"k{idx}"]}} for idx in indices
        ],
    }


class TestNormalizeCardSearchMetadata:
    def test_caps_list_lengths(self) -> None:
        payload = {
            "retrieval_hints": {"bn": [f"h{i}" for i in range(20)]},
            "keywords": {"bn": [f"k{i}" for i in range(20)]},
            "synonyms_en": {f"a{i}": f"expanded {i}" for i in range(20)},
            "questions": {"bn": [f"q{i}" for i in range(20)]},
        }
        out = normalize_card_search_metadata(
            payload,
            max_retrieval_hints=3,
            max_keywords=2,
            max_synonyms=2,
            max_questions=2,
        )
        assert len(out["retrieval_hints"]["bn"]) == 3
        assert len(out["keywords"]["bn"]) == 2
        assert len(out["synonyms_en"]) == 2
        assert len(out["questions"]["bn"]) == 2

    def test_drops_empty_and_duplicate_strings(self) -> None:
        payload = {
            "keywords": {"bn": ["ARI", "  ", "ARI", "cough"]},
            "retrieval_hints": {"bn": []},
        }
        out = normalize_card_search_metadata(
            payload,
            max_retrieval_hints=10,
            max_keywords=10,
            max_synonyms=10,
            max_questions=10,
        )
        assert out["keywords"]["bn"] == ["ARI", "cough"]

    def test_metadata_has_searchable_content(self) -> None:
        normalized = normalize_card_search_metadata(
            _sample_payload(),
            max_retrieval_hints=10,
            max_keywords=10,
            max_synonyms=10,
            max_questions=10,
        )
        assert card_metadata_has_searchable_content(normalized)
        assert not card_metadata_has_searchable_content(
            normalize_card_search_metadata(
                {"keywords": {"bn": []}, "retrieval_hints": {"bn": []}, "synonyms_en": {}},
                max_retrieval_hints=10,
                max_keywords=10,
                max_synonyms=10,
                max_questions=10,
            )
        )


class TestParseBatchCardSearchMetadata:
    def test_parses_all_requested_cards(self) -> None:
        metadata_by_index, failed = parse_batch_card_search_metadata(
            _batch_payload(indices=[0, 1]),
            requested_indices={0, 1},
            max_retrieval_hints=10,
            max_keywords=10,
            max_synonyms=10,
            max_questions=10,
        )
        assert failed == []
        assert set(metadata_by_index) == {0, 1}
        assert metadata_by_index[0]["keywords"]["bn"] == ["k0"]

    def test_marks_missing_index_as_failed(self) -> None:
        metadata_by_index, failed = parse_batch_card_search_metadata(
            _batch_payload(indices=[0]),
            requested_indices={0, 1},
            max_retrieval_hints=10,
            max_keywords=10,
            max_synonyms=10,
            max_questions=10,
        )
        assert set(metadata_by_index) == {0}
        assert failed == [1]

    def test_marks_empty_card_entry_as_failed(self) -> None:
        metadata_by_index, failed = parse_batch_card_search_metadata(
            {"cards": [{"card_index": 0}]},
            requested_indices={0},
            max_retrieval_hints=10,
            max_keywords=10,
            max_synonyms=10,
            max_questions=10,
        )
        assert metadata_by_index == {}
        assert failed == [0]

    def test_invalid_cards_array_fails_all(self) -> None:
        metadata_by_index, failed = parse_batch_card_search_metadata(
            {"cards": "not-a-list"},
            requested_indices={0, 1},
            max_retrieval_hints=10,
            max_keywords=10,
            max_synonyms=10,
            max_questions=10,
        )
        assert metadata_by_index == {}
        assert failed == [0, 1]


def _module(*, cards: list[dict] | None = None) -> Module:
    return Module(
        module_family_id=uuid4(),
        version=1,
        title_localized={"bn": "শিরোনাম", "en": "ARI module"},
        domain="rmnch",
        module_type="refresher",
        module_json={
            "cards": cards
            or [
                {"title": {"bn": "কার্ড ১"}, "body": {"bn": "বিষয়বস্তু ১"}},
                {"title": {"bn": "কার্ড ২"}, "body": {"bn": "বিষয়বস্তু ২"}},
            ]
        },
    )


def _inference_response(payload: dict) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-card-meta",
        generation_type=GenerationType.CARD_SEARCH_METADATA,
        provider="openai",
        model="gpt-4o-mini",
        raw_text="",
        parsed_json=payload,
        latency_ms=1,
        token_usage=TokenUsage(input=1, output=1),
    )


class TestCardSearchMetadataGenerator:
    @pytest.mark.asyncio
    async def test_generate_for_module_uses_batch_template(self) -> None:
        module = _module()
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=_inference_response(
                _batch_payload(indices=[0, 1]),
            )
        )
        generator = CardSearchMetadataGenerator(client=client)
        result = await generator.generate_for_module(module, [0, 1])
        assert result.failed_indices == []
        assert result.metadata_by_index[0]["keywords"]["bn"] == ["k0"]
        assert result.metadata_by_index[1]["keywords"]["bn"] == ["k1"]
        sent = client.generate.call_args[0][0]
        assert sent.generation_type == GenerationType.CARD_SEARCH_METADATA
        assert sent.prompt.template_version == CARD_SEARCH_METADATA_TEMPLATE_VERSION

    @pytest.mark.asyncio
    async def test_generate_wrapper_delegates_to_batch(self) -> None:
        module = _module()
        card = module.module_json["cards"][0]
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_inference_response(_batch_payload(indices=[0])))
        generator = CardSearchMetadataGenerator(client=client)
        result = await generator.generate(module, card, card_index=0)
        assert result.metadata is not None
        assert result.metadata["keywords"]["bn"] == ["k0"]

    @pytest.mark.asyncio
    async def test_returns_error_on_llm_failure(self) -> None:
        module = _module()
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=InferenceResponse(
                request_id="r-card-meta",
                generation_type=GenerationType.CARD_SEARCH_METADATA,
                provider="openai",
                model="gpt-4o-mini",
                raw_text="",
                error="provider down",
                latency_ms=1,
                token_usage=TokenUsage(input=1, output=1),
            )
        )
        generator = CardSearchMetadataGenerator(client=client)
        result = await generator.generate_for_module(module, [0, 1])
        assert result.metadata_by_index == {}
        assert result.failed_indices == [0, 1]
        assert result.error == "provider down"

    @pytest.mark.asyncio
    async def test_returns_error_on_empty_metadata(self) -> None:
        module = _module()
        card = module.module_json["cards"][0]
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_inference_response({"cards": [{"card_index": 0}]}))
        generator = CardSearchMetadataGenerator(client=client)
        result = await generator.generate(module, card, card_index=0)
        assert result.metadata is None
        assert result.error == "empty_metadata"
