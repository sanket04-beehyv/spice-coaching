"""Unit tests for GoogleProvider adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("google.genai")

from ai_runtime.providers.google import GoogleProvider  # noqa: E402


@pytest.mark.asyncio
async def test_generate_returns_text_and_token_counts() -> None:
    client = MagicMock()
    usage = SimpleNamespace(prompt_token_count=12, candidates_token_count=8)
    client.aio.models.generate_content = AsyncMock(
        return_value=SimpleNamespace(text='{"ok": true}', usage_metadata=usage)
    )
    provider = GoogleProvider(client=client)

    raw, input_tok, output_tok = await provider.generate(
        system_prompt="sys",
        human_message="user",
        model="gemini-2.5-flash",
        max_tokens=100,
        temperature=0.2,
        output_format="json",
    )

    assert raw == '{"ok": true}'
    assert input_tok == 12
    assert output_tok == 8


@pytest.mark.asyncio
async def test_embed_returns_vectors() -> None:
    client = MagicMock()
    client.aio.models.embed_content = AsyncMock(
        return_value=SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.1, 0.2]), SimpleNamespace(values=[0.3, 0.4])]
        )
    )
    provider = GoogleProvider(client=client)

    vectors = await provider.embed(["a", "b"], model="gemini-embedding-001")

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


@pytest.mark.asyncio
async def test_aclose_awaits_async_close() -> None:
    client = MagicMock()
    client.close = AsyncMock()
    provider = GoogleProvider(client=client)

    await provider.aclose()

    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_calls_sync_close() -> None:
    client = MagicMock()
    client.close = MagicMock()
    provider = GoogleProvider(client=client)

    await provider.aclose()

    client.close.assert_called_once()
