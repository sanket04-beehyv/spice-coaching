"""Tests for module_text_for_search metadata enrichment."""

from __future__ import annotations

from uuid import uuid4

from platform_service.db.models.module import Module
from platform_service.services.module_search_text import (
    card_metadata_text_for_search,
    metadata_text_for_search,
    module_text_for_search,
)


def _module(**overrides) -> Module:
    base = dict(
        module_family_id=uuid4(),
        version=1,
        title_localized={"bn": "শিরোনাম", "en": "ARI module"},
        description_localized={"bn": "বর্ণনা", "en": "Description"},
        domain="rmnch",
        module_type="refresher",
        module_json={"cards": [{"title": {"bn": "কার্ড"}, "body": {"bn": "বিষয়বস্তু"}}]},
    )
    base.update(overrides)
    return Module(**base)


class TestMetadataTextForSearch:
    def test_extracts_lexical_and_structured_fields(self) -> None:
        metadata = {
            "keywords": {"en": ["ARI", "cough"]},
            "search_phrases": {"en": ["child cough more than 14 days"]},
            "synonyms": {"en": {"ARI": "acute respiratory infection"}},
            "topic_tags": ["respiratory"],
            "clinical_conditions": ["pneumonia"],
        }
        parts = metadata_text_for_search(metadata)
        assert "ARI" in parts
        assert "acute respiratory infection" in parts
        assert "child cough more than 14 days" in parts
        assert "respiratory" in parts

    def test_empty_metadata_returns_empty_list(self) -> None:
        assert metadata_text_for_search(None) == []
        assert metadata_text_for_search({}) == []
        assert card_metadata_text_for_search(None) == []
        assert card_metadata_text_for_search({}) == []


class TestCardMetadataTextForSearch:
    def test_extracts_card_lexical_fields(self) -> None:
        metadata = {
            "retrieval_hints": {"en": ["child cough"]},
            "keywords": {"en": ["ARI"]},
            "synonyms": {"en": {"ARI": "acute respiratory infection"}},
            "questions": {"en": ["When to refer?"]},
        }
        parts = card_metadata_text_for_search(metadata)
        assert "child cough" in parts
        assert "ARI" in parts
        assert "acute respiratory infection" in parts
        assert "When to refer?" in parts


class TestModuleTextForSearch:
    def test_includes_metadata_between_description_and_cards(self) -> None:
        module = _module(
            search_metadata_jsonb={
                "keywords": {"en": ["fast breathing"]},
                "search_phrases": {"en": ["breathing rate 40 per minute"]},
            }
        )
        text = module_text_for_search(module)
        title_pos = text.index("ARI module")
        meta_pos = text.index("fast breathing")
        card_pos = text.index("কার্ড")
        assert title_pos < meta_pos < card_pos

    def test_without_metadata_matches_titles_and_cards(self) -> None:
        module = _module(search_metadata_jsonb=None)
        text = module_text_for_search(module)
        assert "ARI module" in text
        assert "কার্ড" in text
        assert "fast breathing" not in text

    def test_includes_description_en(self) -> None:
        module = _module()
        text = module_text_for_search(module)
        assert "Description" in text

    def test_includes_card_search_metadata(self) -> None:
        module = _module(
            module_json={
                "cards": [
                    {
                        "title": {"bn": "কার্ড"},
                        "body": {"bn": "বিষয়বস্তু"},
                        "search_metadata": {
                            "keywords": {"en": ["fast breathing"]},
                            "questions": {"en": ["breathing rate threshold?"]},
                        },
                    }
                ]
            }
        )
        text = module_text_for_search(module)
        assert "fast breathing" in text
        assert "breathing rate threshold?" in text
