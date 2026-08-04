"""Direct tests for search metadata normalization and generator."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
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


def _sample_payload(*, primary: str = "bn") -> dict:
    return {
        "schema_version": 1,
        "keywords": {primary: ["ARI", "cough", "fast breathing"]},
        "search_phrases": {primary: ["child cough more than 14 days"]},
        "synonyms": {primary: {"ARI": "acute respiratory infection"}},
        "topic_tags": {primary: ["respiratory", "child_health"]},
        "clinical_conditions": {primary: ["pneumonia"]},
        "audience": "chw_field_worker",
        "rationale": "Covers ARI symptom thresholds.",
    }


class TestNormalizeSearchMetadata:
    def test_caps_list_lengths(self) -> None:
        payload = {
            "keywords": {"bn": [f"k{i}" for i in range(20)]},
            "search_phrases": {"bn": [f"p{i}" for i in range(20)]},
            "synonyms": {"bn": {f"a{i}": f"expanded {i}" for i in range(20)}},
            "topic_tags": {"bn": [f"t{i}" for i in range(20)]},
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
        assert len(out["synonyms"]["bn"]) == 2
        assert len(out["topic_tags"]["bn"]) == 2

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

    def test_coerces_legacy_plain_topic_tags_list(self) -> None:
        out = normalize_search_metadata(
            {"topic_tags": ["respiratory", "child_health"]},
            max_keywords=10,
            max_search_phrases=10,
            max_synonyms=10,
            max_tags=10,
        )
        assert out["topic_tags"]["bn"] == ["respiratory", "child_health"]

    def test_coerces_legacy_plain_clinical_conditions_list(self) -> None:
        out = normalize_search_metadata(
            {"clinical_conditions": ["pneumonia", "ari"]},
            max_keywords=10,
            max_search_phrases=10,
            max_synonyms=10,
            max_tags=10,
        )
        assert out["clinical_conditions"]["bn"] == ["pneumonia", "ari"]

    def test_migrates_legacy_synonyms_en(self) -> None:
        out = normalize_search_metadata(
            {"synonyms_en": {"ARI": "acute respiratory infection", "cough": "cough"}},
            max_keywords=10,
            max_search_phrases=10,
            max_synonyms=10,
            max_tags=10,
        )
        assert out["synonyms"]["bn"]["ARI"] == "acute respiratory infection"

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
                {
                    "keywords": {"bn": []},
                    "search_phrases": {"bn": []},
                    "synonyms": {"bn": {}},
                },
                max_keywords=10,
                max_search_phrases=10,
                max_synonyms=10,
                max_tags=10,
            )
        )

    @pytest.mark.parametrize("primary", ["hi", "bn"])
    def test_writes_only_deployment_primary_locale(self, primary: str) -> None:
        payload = {
            "keywords": {"hi": ["खांसी"], "bn": ["কাশি"], "en": ["cough"]},
            "search_phrases": {"hi": ["बच्चे की खांसी"], "bn": ["শিশুর কাশি"]},
            "topic_tags": {"hi": ["respiratory"], "bn": ["শ্বাস"]},
            "synonyms": {
                "hi": {"ARI": "खांसी संक्रमण"},
                "bn": {"ARI": "তীব্র শ্বাসতন্ত্রের সংক্রমণ"},
            },
            "clinical_conditions": {"hi": ["निमोनिया"], "bn": ["নিউমোনিয়া"]},
        }
        with patch(
            "platform_service.services.module_search_metadata_generator.get_settings",
            return_value=__import__("platform_service.config", fromlist=["Settings"]).Settings(
                deployment_primary_locale=primary
            ),
        ):
            out = normalize_search_metadata(
                payload,
                max_keywords=10,
                max_search_phrases=10,
                max_synonyms=10,
                max_tags=10,
                primary_locale=primary,
            )
        assert primary in out["keywords"]
        assert list(out["keywords"].keys()) == [primary]
        assert list(out["topic_tags"].keys()) == [primary]
        assert list(out["synonyms"].keys()) == [primary]
        assert list(out["clinical_conditions"].keys()) == [primary]


def _inference_response(*, primary: str = "bn") -> InferenceResponse:
    return InferenceResponse(
        request_id="r-meta",
        generation_type=GenerationType.MODULE_SEARCH_METADATA,
        provider="google",
        model="gemini-2.5-flash",
        max_tokens=8192,
        temperature=0.2,
        raw_text="",
        parsed_json=_sample_payload(primary=primary),
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
        assert "ARI" in result.metadata["keywords"]["bn"]
        assert "ARI" in result.metadata["synonyms"]["bn"]
        sent = client.generate.await_args.args[0]
        assert sent.generation_type == GenerationType.MODULE_SEARCH_METADATA

    @pytest.mark.asyncio
    async def test_generate_returns_error_on_runtime_failure(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(
            return_value=InferenceResponse(
                request_id="r-meta",
                generation_type=GenerationType.MODULE_SEARCH_METADATA,
                provider="google",
                model="gemini-2.5-flash",
                max_tokens=8192,
                temperature=0.2,
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
