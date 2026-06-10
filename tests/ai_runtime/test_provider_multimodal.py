"""W-AI-RUNTIME unit tests — GoogleProvider call construction (google-genai SDK).

We inject a mock genai.Client via the provider's `client=` constructor arg so
tests don't need google-genai to be configured for either Vertex or the
Developer API. Assertions cover:
- text-only calls don't add image parts
- multimodal calls send Part.from_bytes for each image
- system prompt rides on GenerateContentConfig.system_instruction (not as a
  message part)
- embedding calls forward task_type and output dimensionality
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from ai_runtime.providers.base import ProviderImage
from ai_runtime.providers.google import GoogleProvider
from google.genai import types


def _make_mock_client(
    *,
    raw_text: str = "{}",
    input_tok: int = 100,
    output_tok: int = 50,
) -> MagicMock:
    """Build a mock genai.Client with .aio.models.generate_content() and
    .aio.models.embed_content() pre-wired to AsyncMocks."""
    response = SimpleNamespace(
        text=raw_text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=input_tok,
            candidates_token_count=output_tok,
        ),
    )
    aio_models = MagicMock()
    aio_models.generate_content = AsyncMock(return_value=response)
    # embed_content default — overridden per-test where needed.
    aio_models.embed_content = AsyncMock(
        return_value=SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1, 0.2, 0.3])])
    )
    client = MagicMock()
    client.aio = SimpleNamespace(models=aio_models)
    return client


# ── Construction / auth-mode validation ──────────────────────────────────


class TestGoogleProviderConstruction:
    def test_developer_api_requires_real_key(self) -> None:
        with pytest.raises(ValueError, match="Developer API mode requires a real api_key"):
            GoogleProvider(api_key="")

    def test_developer_api_rejects_placeholder_key(self) -> None:
        with pytest.raises(ValueError, match="Developer API mode requires a real api_key"):
            GoogleProvider(api_key="default=key")

    def test_vertex_mode_requires_project(self) -> None:
        with pytest.raises(ValueError, match="Vertex AI mode requires a project id"):
            GoogleProvider(use_vertex=True)

    def test_injected_client_bypasses_auth(self) -> None:
        # Should not raise.
        GoogleProvider(client=_make_mock_client())


# ── Text-only generate() ────────────────────────────────────────────────


class TestGoogleProviderTextOnly:
    @pytest.mark.asyncio
    async def test_text_only_call_passes_only_human_message(self) -> None:
        client = _make_mock_client(raw_text='{"ok": true}', input_tok=12, output_tok=7)
        provider = GoogleProvider(client=client)
        raw, in_tok, out_tok = await provider.generate(
            system_prompt="You are a helper",
            human_message="hello",
            model="gemini-2.5-flash",
            max_tokens=512,
            temperature=0.2,
        )
        assert raw == '{"ok": true}'
        assert in_tok == 12
        assert out_tok == 7

        client.aio.models.generate_content.assert_awaited_once()
        call_kwargs = client.aio.models.generate_content.call_args.kwargs
        assert call_kwargs["model"] == "gemini-2.5-flash"
        # Single text part, no Part.from_bytes
        assert call_kwargs["contents"] == ["hello"]
        cfg = call_kwargs["config"]
        assert isinstance(cfg, types.GenerateContentConfig)
        assert cfg.system_instruction == "You are a helper"
        assert cfg.max_output_tokens == 512
        assert cfg.temperature == 0.2
        assert cfg.response_mime_type == "application/json"

    @pytest.mark.asyncio
    async def test_text_only_with_empty_images_list(self) -> None:
        client = _make_mock_client()
        provider = GoogleProvider(client=client)
        await provider.generate(
            system_prompt="sys",
            human_message="msg",
            model="gemini-2.5-flash",
            max_tokens=256,
            temperature=0.0,
            images=[],
        )
        assert client.aio.models.generate_content.call_args.kwargs["contents"] == ["msg"]

    @pytest.mark.asyncio
    async def test_text_only_with_images_none(self) -> None:
        client = _make_mock_client()
        provider = GoogleProvider(client=client)
        await provider.generate(
            system_prompt="sys",
            human_message="msg",
            model="gemini-2.5-flash",
            max_tokens=256,
            temperature=0.0,
            images=None,
        )
        assert client.aio.models.generate_content.call_args.kwargs["contents"] == ["msg"]


# ── Multimodal generate() ───────────────────────────────────────────────


class TestGoogleProviderMultimodal:
    @pytest.mark.asyncio
    async def test_single_image_call_uses_part_from_bytes(self) -> None:
        client = _make_mock_client(raw_text="extracted markdown")
        provider = GoogleProvider(client=client)
        img = ProviderImage(data=b"PNG_BYTES", mime_type="image/png", label="page_1")

        raw, _, _ = await provider.generate(
            system_prompt="extract verbatim",
            human_message="describe this page",
            model="gemini-2.5-flash",
            max_tokens=4096,
            temperature=0.1,
            images=[img],
        )
        assert raw == "extracted markdown"
        contents = client.aio.models.generate_content.call_args.kwargs["contents"]
        assert len(contents) == 2
        assert contents[0] == "describe this page"
        # Second element is a google.genai Part with inline_data populated.
        part = contents[1]
        assert isinstance(part, types.Part)
        assert part.inline_data is not None
        assert part.inline_data.mime_type == "image/png"
        assert part.inline_data.data == b"PNG_BYTES"

    @pytest.mark.asyncio
    async def test_multiple_image_call_preserves_order(self) -> None:
        client = _make_mock_client()
        provider = GoogleProvider(client=client)
        imgs = [
            ProviderImage(data=b"A", mime_type="image/png"),
            ProviderImage(data=b"B", mime_type="image/jpeg"),
            ProviderImage(data=b"C", mime_type="image/png"),
        ]
        await provider.generate(
            system_prompt="sys",
            human_message="multi",
            model="gemini-2.5-flash",
            max_tokens=512,
            temperature=0.0,
            images=imgs,
        )
        contents = client.aio.models.generate_content.call_args.kwargs["contents"]
        assert len(contents) == 4  # 1 text + 3 images
        assert contents[0] == "multi"
        assert contents[1].inline_data.data == b"A"
        assert contents[1].inline_data.mime_type == "image/png"
        assert contents[2].inline_data.data == b"B"
        assert contents[2].inline_data.mime_type == "image/jpeg"
        assert contents[3].inline_data.data == b"C"

    @pytest.mark.asyncio
    async def test_system_prompt_lives_on_config_not_in_contents(self) -> None:
        client = _make_mock_client()
        provider = GoogleProvider(client=client)
        img = ProviderImage(data=b"x", mime_type="image/png")
        await provider.generate(
            system_prompt="THIS_IS_SYSTEM",
            human_message="user msg",
            model="gemini-2.5-flash",
            max_tokens=512,
            temperature=0.0,
            images=[img],
        )
        kwargs = client.aio.models.generate_content.call_args.kwargs
        assert kwargs["config"].system_instruction == "THIS_IS_SYSTEM"
        # System text is NOT in contents list
        assert "THIS_IS_SYSTEM" not in kwargs["contents"]


# ── embed() ─────────────────────────────────────────────────────────────


class TestGoogleProviderEmbed:
    @pytest.mark.asyncio
    async def test_embed_returns_list_of_lists(self) -> None:
        client = _make_mock_client()
        client.aio.models.embed_content = AsyncMock(
            return_value=SimpleNamespace(
                embeddings=[
                    SimpleNamespace(values=[0.1, 0.2]),
                    SimpleNamespace(values=[0.3, 0.4]),
                ]
            )
        )
        provider = GoogleProvider(client=client)
        out = await provider.embed(["a", "b"], model="gemini-embedding-001")
        assert out == [[0.1, 0.2], [0.3, 0.4]]
        kwargs = client.aio.models.embed_content.call_args.kwargs
        assert kwargs["model"] == "gemini-embedding-001"
        assert kwargs["contents"] == ["a", "b"]
        assert isinstance(kwargs["config"], types.EmbedContentConfig)
        assert kwargs["config"].task_type == "RETRIEVAL_DOCUMENT"

    @pytest.mark.asyncio
    async def test_embed_forwards_output_dimensionality_when_set(self) -> None:
        client = _make_mock_client()
        provider = GoogleProvider(client=client, embedding_dimension=768)
        await provider.embed(["x"], model="gemini-embedding-001")
        cfg = client.aio.models.embed_content.call_args.kwargs["config"]
        assert cfg.output_dimensionality == 768

    @pytest.mark.asyncio
    async def test_embed_omits_output_dimensionality_when_unset(self) -> None:
        client = _make_mock_client()
        provider = GoogleProvider(client=client)
        await provider.embed(["x"], model="gemini-embedding-001")
        cfg = client.aio.models.embed_content.call_args.kwargs["config"]
        assert cfg.output_dimensionality is None
