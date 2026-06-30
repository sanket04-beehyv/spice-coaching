"""Direct tests for search metadata normalization and generator."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceResponse, TokenUsage
from platform_service.db.models.module import Module
from platform_service.services.module_search_metadata_generator import (
    ModuleSearchMetadataGenerator,
    metadata_has_searchable_content,
    normalize_search_metadata,
)


def _sample_payload() -> dict:
    return {
        "schema_version": 1,
        "keywords": {"bn": ["কাশি"], "en": ["ARI", "cough", "fast breathing"]},
        "search_phrases": {
            "bn": ["১৪ দিনের বেশি কাশি"],
            "en": ["child cough more than 14 days"],
        },
        "synonyms_en": {"ARI": "acute respiratory infection"},
        "topic_tags": ["respiratory", "child_health"],
        "clinical_conditions": ["pneumonia"],
        "audience": "chw_field_worker",
        "rationale": "Covers ARI symptom thresholds.",
    }


class TestNormalizeSearchMetadata:
    def test_caps_list_lengths(self) -> None:
        payload = {
            "keywords": {"bn": [f"k{i}" for i in range(20)]},
            "search_phrases": {"bn": [f"p{i}" for i in range(20)]},
            "synonyms_en": {f"a{i}": f"expanded {i}" for i in range(20)},
            "topic_tags": [f"t{i}" for i in range(20)],
        }
        out = normalize_search_metadata(
            payload,
            max_keywords=3,
            max_search_phrases=2,
            max_synonyms=2,
            max_tags=2,
        )
        assert len(out["keywords"]["bn"]) == 3
        assert len(out["search_phrases"]["bn"]) == 2
        assert len(out["synonyms_en"]) == 2
        assert len(out["topic_tags"]) == 2

    def test_drops_empty_and_duplicate_strings(self) -> None:
        payload = {
            "keywords": {"bn": ["ARI", "  ", "ARI", "cough"]},
            "search_phrases": {"bn": []},
        }
        out = normalize_search_metadata(
            payload,
            max_keywords=10,
            max_search_phrases=10,
            max_synonyms=10,
            max_tags=10,
        )
        assert out["keywords"]["bn"] == ["ARI", "cough"]

    def test_metadata_has_searchable_content(self) -> None:
        assert metadata_has_searchable_content(
            normalize_search_metadata(
                _sample_payload(),
                max_keywords=10,
                max_search_phrases=10,
                max_synonyms=10,
                max_tags=10,
            )
        )
        assert not metadata_has_searchable_content(
            normalize_search_metadata(
                {"keywords": {"bn": []}, "search_phrases": {"bn": []}, "synonyms_en": {}},
                max_keywords=10,
                max_search_phrases=10,
                max_synonyms=10,
                max_tags=10,
            )
        )


def _inference_response() -> InferenceResponse:
    return InferenceResponse(
        request_id="r-meta",
        generation_type=GenerationType.MODULE_SEARCH_METADATA,
        provider="openai",
        model="gpt-4o-mini",
        raw_text="",
        parsed_json=_sample_payload(),
        latency_ms=1,
        token_usage=TokenUsage(input=1, output=1),
    )


class TestModuleSearchMetadataGenerator:
    @pytest.mark.asyncio
    async def test_generate_persists_normalized_metadata(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_inference_response())
        generator = ModuleSearchMetadataGenerator(client=client)
        module = Module(
            module_family_id=uuid4(),
            version=1,
            title_localized={"bn": "শিরোনাম", "en": "ARI module"},
            domain="rmnch",
            module_type="refresher",
            module_json={"cards": [{"title": {"bn": "T"}, "body": {"bn": "body"}}]},
        )

        result = await generator.generate(module)

        assert result.error is None
        assert result.metadata is not None
        assert "কাশি" in result.metadata["keywords"]["bn"]
        sent = client.generate.await_args.args[0]
        assert sent.generation_type == GenerationType.MODULE_SEARCH_METADATA

    @pytest.mark.asyncio
    async def test_generate_returns_error_on_runtime_failure(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=InferenceResponse(
                request_id="r-meta",
                generation_type=GenerationType.MODULE_SEARCH_METADATA,
                provider="openai",
                model="gpt-4o-mini",
                raw_text="",
                parsed_json=None,
                latency_ms=1,
                token_usage=TokenUsage(input=1, output=1),
                error="provider down",
            )
        )
        generator = ModuleSearchMetadataGenerator(client=client)
        module = Module(
            module_family_id=uuid4(),
            version=1,
            title_localized={"bn": "t"},
            domain="rmnch",
            module_type="refresher",
            module_json={"cards": []},
        )

        result = await generator.generate(module)

        assert result.metadata is None
        assert result.error == "provider down"
