"""Tests for eval corpus card text assembly."""

from __future__ import annotations

from eval.rag.corpus import card_text_for_search


class TestCardTextForSearch:
    def test_includes_card_search_metadata(self) -> None:
        card = {
            "title": {"bn": "কার্ড"},
            "body": {"bn": "বিষয়বস্তু"},
            "search_metadata": {
                "keywords": {"bn": ["fast breathing"]},
                "questions": {"bn": ["breathing rate threshold?"]},
            },
        }
        text = card_text_for_search(card)
        assert "কার্ড" in text
        assert "fast breathing" in text
        assert "breathing rate threshold?" in text
