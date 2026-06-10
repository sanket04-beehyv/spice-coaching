"""W-5 — translator unit tests."""

from unittest.mock import AsyncMock

import pytest
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceRequest, InferenceResponse
from platform_service.services.translator import (
    Translator,
    TranslatorError,
    numerical_values_preserved,
)


def _resp(*, raw_text: str = "", error: str | None = None) -> InferenceResponse:
    return InferenceResponse(
        request_id="r-1",
        generation_type=GenerationType.BILINGUAL_TRANSLATION,
        provider="google",
        model="gemini-2.5-flash",
        raw_text=raw_text,
        latency_ms=100,
        error=error,
    )


class TestTranslator:
    @pytest.mark.asyncio
    async def test_returns_translated_text(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(raw_text="বাংলায় অনুবাদ"))
        t = Translator(client=client)
        out = await t.translate(source_text="hello", target_language="bn")
        assert out == "বাংলায় অনুবাদ"

    @pytest.mark.asyncio
    async def test_uses_bilingual_translation_generation_type(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(raw_text="x"))
        t = Translator(client=client)
        await t.translate(source_text="hello", target_language="bn")
        sent: InferenceRequest = client.generate.call_args.args[0]
        assert sent.generation_type == GenerationType.BILINGUAL_TRANSLATION

    @pytest.mark.asyncio
    async def test_empty_source_returns_empty(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock()
        t = Translator(client=client)
        assert await t.translate(source_text="", target_language="bn") == ""
        assert await t.translate(source_text="   ", target_language="en") == ""
        client.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsupported_target_language_raises(self) -> None:
        t = Translator(client=AsyncMock())
        with pytest.raises(TranslatorError, match="Unsupported"):
            await t.translate(source_text="hi", target_language="fr")

    @pytest.mark.asyncio
    async def test_runtime_error_raises(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(error="boom"))
        t = Translator(client=client)
        with pytest.raises(TranslatorError, match="boom"):
            await t.translate(source_text="hi", target_language="bn")

    @pytest.mark.asyncio
    async def test_empty_response_raises(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(raw_text="   "))
        t = Translator(client=client)
        with pytest.raises(TranslatorError, match="empty"):
            await t.translate(source_text="hi", target_language="bn")

    @pytest.mark.asyncio
    async def test_glossary_passed_in_payload(self) -> None:
        client = AsyncMock()
        client.generate = AsyncMock(return_value=_resp(raw_text="x"))
        t = Translator(client=client)
        await t.translate(
            source_text="anaemia",
            target_language="bn",
            glossary=[{"term_en": "anaemia", "term_bn": "রক্তস্বল্পতা"}],
        )
        sent: InferenceRequest = client.generate.call_args.args[0]
        assert "রক্তস্বল্পতা" in sent.prompt.resolved_human_message


class TestNumericalPreservation:
    def test_simple_match(self) -> None:
        assert numerical_values_preserved("BP 140/90", "বিপি 140/90")

    def test_missing_number_fails(self) -> None:
        assert not numerical_values_preserved("BP 140/90", "বিপি high")

    def test_no_numbers_in_either_passes(self) -> None:
        assert numerical_values_preserved("hello", "নমস্কার")

    def test_extra_number_in_translation_ok(self) -> None:
        # Translator may add numbers (e.g. for clarity); we only fail on missing.
        assert numerical_values_preserved("BP 140", "BP 140 (page 5)")
