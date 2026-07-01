"""Unit tests for quiz explanation card-citation stripping."""

from __future__ import annotations

from platform_service.services.quiz_explanation_sanitizer import (
    strip_card_citations_from_explanation,
)


class TestStripCardCitationsFromExplanation:
    def test_no_op_when_no_citation(self) -> None:
        text = "Refer the patient immediately because BP exceeds the threshold."
        assert strip_card_citations_from_explanation(text) == text

    def test_strips_bare_english_card_ref(self) -> None:
        result = strip_card_citations_from_explanation(
            "This is correct because referral is required, Card 2."
        )
        assert result == "This is correct because referral is required."

    def test_strips_see_card_phrase(self) -> None:
        result = strip_card_citations_from_explanation(
            "The answer is iron supplementation. See Card 1 for details."
        )
        assert result == "The answer is iron supplementation."

    def test_strips_parenthetical_card_ref(self) -> None:
        result = strip_card_citations_from_explanation(
            "Refer immediately (Card 3) when danger signs are present."
        )
        assert result == "Refer immediately when danger signs are present."

    def test_strips_according_to_card(self) -> None:
        result = strip_card_citations_from_explanation(
            "According to Card 4, oxytocin is given after delivery."
        )
        assert result == "oxytocin is given after delivery."

    def test_strips_as_stated_in_card(self) -> None:
        result = strip_card_citations_from_explanation("As stated in Card 2, the threshold is 140/90.")
        assert result == "the threshold is 140/90."

    def test_strips_card_states_phrase(self) -> None:
        result = strip_card_citations_from_explanation("Card 1 states that early referral reduces mortality.")
        assert result == "that early referral reduces mortality."

    def test_strips_bengali_card_ref(self) -> None:
        result = strip_card_citations_from_explanation("রেফার করুন কারণ বিপদ সংকেত আছে, কার্ড ২ অনুযায়ী।")
        assert result == "রেফার করুন কারণ বিপদ সংকেত আছে।"

    def test_strips_bengali_card_locative(self) -> None:
        result = strip_card_citations_from_explanation("কার্ড ৩-এ বলা হয়েছে যে আয়রন সাপ্লিমেন্ট দিতে হবে।")
        assert result == "বলা হয়েছে যে আয়রন সাপ্লিমেন্ট দিতে হবে।"

    def test_collapses_extra_whitespace(self) -> None:
        result = strip_card_citations_from_explanation("Correct answer:   Card 2   confirms   referral.")
        assert result == "Correct answer: confirms referral."

    def test_empty_string_unchanged(self) -> None:
        assert strip_card_citations_from_explanation("") == ""
