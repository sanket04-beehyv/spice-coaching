"""W-AI-RUNTIME unit tests — PromptExecutor v3.3 behaviours.

Verifies:
- Base64 image_attachments decode to ProviderImage and forward to provider
- Malformed base64 returns InferenceResponse.error without invoking provider
- New generation types (vision_extraction, module_identification, etc.)
  dispatch through the same execute() path
- parsed_json widened to accept lists from list-returning generation types
- Provider exceptions surface as InferenceResponse.error
"""

import base64
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

# `ai_runtime.providers.openai` imports `from openai import AsyncOpenAI` at
# module load. The `openai` SDK is a workspace-level dep installed via
# `uv sync --all-packages`; if a dev runs `pytest` without it, the bare
# import would error during collection — skip the whole module instead.
pytest.importorskip("openai")

from ai_runtime.providers.base import BaseProvider, ProviderImage  # noqa: E402
from ai_runtime.services.prompt_executor import PromptExecutor  # noqa: E402
from mc_contracts.enums import GenerationType  # noqa: E402
from mc_contracts.internal_ai import (  # noqa: E402
    GenerationConstraints,
    InferenceImage,
    InferenceRequest,
    ModelPolicy,
    PromptSpec,
)


class _StubProvider(BaseProvider):
    """In-memory provider used to assert what the executor passes through."""

    def __init__(
        self,
        raw_text: str = '{"ok": true}',
        input_tokens: int = 10,
        output_tokens: int = 5,
        raise_on_generate: Exception | None = None,
    ) -> None:
        self.raw_text = raw_text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.raise_on_generate = raise_on_generate
        self.generate_calls: list[dict[str, Any]] = []

    async def generate(
        self,
        system_prompt: str,
        human_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        images: list[ProviderImage] | None = None,
        output_format: str = "json",
    ) -> tuple[str, int, int]:
        self.generate_calls.append(
            {
                "system_prompt": system_prompt,
                "human_message": human_message,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "images": images,
                "output_format": output_format,
            }
        )
        if self.raise_on_generate is not None:
            raise self.raise_on_generate
        return self.raw_text, self.input_tokens, self.output_tokens

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        return [[0.0] * 768 for _ in texts]

    async def transcribe_media(self, media_bytes: bytes, mime_type: str, model: str) -> str:
        return ""


@pytest.fixture
def stub_provider() -> _StubProvider:
    return _StubProvider()


@pytest.fixture
def patched_executor(stub_provider: _StubProvider) -> Iterator[PromptExecutor]:
    """PromptExecutor with the provider factory swapped for the stub."""
    with patch("ai_runtime.services.prompt_executor._get_provider", return_value=stub_provider):
        yield PromptExecutor()


def _make_request(
    *,
    generation_type: GenerationType = GenerationType.CARD_DRAFTING,
    image_attachments: list[InferenceImage] | None = None,
    output_format: str = "json",
) -> InferenceRequest:
    return InferenceRequest(
        request_id="req-1",
        generation_type=generation_type,
        model_policy=ModelPolicy(model="gemini-2.5-flash"),
        prompt=PromptSpec(
            template_id="t-1",
            template_version=1,
            resolved_system_prompt="sys",
            resolved_human_message="user",
        ),
        constraints=GenerationConstraints(language="bn", output_format=output_format),
        image_attachments=image_attachments or [],
    )


# ── Image decoding ──────────────────────────────────────────────────────


class TestImageDecoding:
    @pytest.mark.asyncio
    async def test_base64_attachments_decode_and_forward(
        self, patched_executor: PromptExecutor, stub_provider: _StubProvider
    ) -> None:
        img_bytes = b"\x89PNG\r\n\x1a\nfake png data"
        att = InferenceImage(
            mime_type="image/png",
            data_base64=base64.b64encode(img_bytes).decode(),
            label="page_3",
        )
        req = _make_request(
            generation_type=GenerationType.VISION_EXTRACTION,
            image_attachments=[att],
            output_format="text",
        )
        response = await patched_executor.execute(req)

        assert response.error is None
        # Stub provider received decoded ProviderImage
        assert len(stub_provider.generate_calls) == 1
        call = stub_provider.generate_calls[0]
        assert call["images"] is not None
        assert len(call["images"]) == 1
        assert call["images"][0].data == img_bytes
        assert call["images"][0].mime_type == "image/png"
        assert call["images"][0].label == "page_3"

    @pytest.mark.asyncio
    async def test_no_attachments_passes_none_to_provider(
        self, patched_executor: PromptExecutor, stub_provider: _StubProvider
    ) -> None:
        req = _make_request(generation_type=GenerationType.CARD_DRAFTING)
        await patched_executor.execute(req)
        assert stub_provider.generate_calls[0]["images"] is None

    @pytest.mark.asyncio
    async def test_malformed_base64_returns_error_without_calling_provider(
        self, patched_executor: PromptExecutor, stub_provider: _StubProvider
    ) -> None:
        bad_att = InferenceImage(mime_type="image/png", data_base64="not!valid!base64!!!")
        req = _make_request(
            generation_type=GenerationType.VISION_EXTRACTION,
            image_attachments=[bad_att],
            output_format="text",
        )
        response = await patched_executor.execute(req)

        assert response.error is not None
        assert "base64" in response.error.lower() or "not valid" in response.error.lower()
        assert response.raw_text == ""
        assert stub_provider.generate_calls == []  # provider never called

    @pytest.mark.asyncio
    async def test_multiple_images_all_decoded(
        self, patched_executor: PromptExecutor, stub_provider: _StubProvider
    ) -> None:
        atts = [
            InferenceImage(
                mime_type="image/png",
                data_base64=base64.b64encode(b"PAGE_A").decode(),
            ),
            InferenceImage(
                mime_type="image/jpeg",
                data_base64=base64.b64encode(b"PAGE_B").decode(),
            ),
        ]
        req = _make_request(
            generation_type=GenerationType.VISION_EXTRACTION,
            image_attachments=atts,
            output_format="text",
        )
        await patched_executor.execute(req)
        provider_images = stub_provider.generate_calls[0]["images"]
        assert [pi.data for pi in provider_images] == [b"PAGE_A", b"PAGE_B"]
        assert [pi.mime_type for pi in provider_images] == ["image/png", "image/jpeg"]


# ── New generation type dispatch ─────────────────────────────────────────


class TestNewGenerationTypeDispatch:
    @pytest.mark.parametrize(
        "gt",
        [
            GenerationType.OUTLINE_INFERENCE,
            GenerationType.MODULE_IDENTIFICATION,
            GenerationType.CARD_DRAFTING,
            GenerationType.QUIZ_DRAFTING,
            GenerationType.DISTRACTOR_CRITIQUE,
            GenerationType.BILINGUAL_TRANSLATION,
            GenerationType.VISION_EXTRACTION,
        ],
    )
    @pytest.mark.asyncio
    async def test_v33_types_dispatch_through_executor(
        self,
        gt: GenerationType,
        patched_executor: PromptExecutor,
        stub_provider: _StubProvider,
    ) -> None:
        req = _make_request(generation_type=gt)
        response = await patched_executor.execute(req)
        assert response.generation_type is gt
        assert response.error is None
        assert response.parsed_json == {"ok": True}


# ── Structured output (parsed_json widened) ─────────────────────────────


class TestStructuredOutputParsedJson:
    @pytest.mark.asyncio
    async def test_top_level_array_response_preserved_as_list(self, stub_provider: _StubProvider) -> None:
        """module_identification returns a top-level JSON array of candidates."""
        stub_provider.raw_text = '[{"title": "cand 1"}, {"title": "cand 2"}]'
        with patch(
            "ai_runtime.services.prompt_executor._get_provider",
            return_value=stub_provider,
        ):
            executor = PromptExecutor()
            req = _make_request(generation_type=GenerationType.MODULE_IDENTIFICATION)
            response = await executor.execute(req)
        assert isinstance(response.parsed_json, list)
        assert len(response.parsed_json) == 2

    @pytest.mark.asyncio
    async def test_top_level_object_response_preserved_as_dict(self, stub_provider: _StubProvider) -> None:
        stub_provider.raw_text = '{"cards": [{"id": "c1"}], "quiz": []}'
        with patch(
            "ai_runtime.services.prompt_executor._get_provider",
            return_value=stub_provider,
        ):
            executor = PromptExecutor()
            req = _make_request(generation_type=GenerationType.CARD_DRAFTING)
            response = await executor.execute(req)
        assert isinstance(response.parsed_json, dict)
        assert "cards" in response.parsed_json

    @pytest.mark.asyncio
    async def test_text_output_format_skips_json_parse(self, stub_provider: _StubProvider) -> None:
        stub_provider.raw_text = "raw text response"
        with patch(
            "ai_runtime.services.prompt_executor._get_provider",
            return_value=stub_provider,
        ):
            executor = PromptExecutor()
            req = _make_request(generation_type=GenerationType.VISION_EXTRACTION, output_format="text")
            response = await executor.execute(req)
        assert response.raw_text == "raw text response"
        assert response.parsed_json is None


# ── Error paths ──────────────────────────────────────────────────────────


class TestErrorPaths:
    @pytest.mark.asyncio
    async def test_provider_exception_returns_error_response(self) -> None:
        stub = _StubProvider(raise_on_generate=RuntimeError("provider boom"))
        with patch("ai_runtime.services.prompt_executor._get_provider", return_value=stub):
            executor = PromptExecutor()
            req = _make_request(generation_type=GenerationType.CARD_DRAFTING)
            response = await executor.execute(req)
        assert response.error == "provider boom"
        assert response.raw_text == ""
        assert response.parsed_json is None
        assert response.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_json_parse_failure_sets_error_field(self, stub_provider: _StubProvider) -> None:
        stub_provider.raw_text = "not valid json at all"
        with patch(
            "ai_runtime.services.prompt_executor._get_provider",
            return_value=stub_provider,
        ):
            executor = PromptExecutor()
            executor._settings = SimpleNamespace(
                ai_provider="google",
                default_max_tokens=8192,
                default_temperature=0.2,
                json_parse_retries=0,
            )
            response = await executor.execute(_make_request())
        assert response.parsed_json is None
        assert response.error == "failed to parse JSON from provider output"
        assert response.raw_text == "not valid json at all"

    @pytest.mark.asyncio
    async def test_execute_uses_configured_provider_not_request_model_only(
        self, stub_provider: _StubProvider
    ) -> None:
        """Provider routing is ai-runtime config; model comes from the request."""
        with patch(
            "ai_runtime.services.prompt_executor._get_provider",
            return_value=stub_provider,
        ) as get_provider:
            executor = PromptExecutor()
            executor._settings = SimpleNamespace(
                ai_provider="google",
                default_max_tokens=8192,
                default_temperature=0.2,
                json_parse_retries=0,
            )
            response = await executor.execute(_make_request())
        get_provider.assert_called_once_with("google")
        assert response.provider == "google"
