"""Unit tests for OpenAIProvider adapter."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("openai")

from ai_runtime.providers.openai import OpenAIProvider  # noqa: E402


@pytest.mark.asyncio
async def test_generate_returns_first_choice_content() -> None:
    client = MagicMock()
    choice = SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7)
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[choice], usage=usage))
    provider = OpenAIProvider(api_key="test-key")
    provider._client = client

    raw, input_tok, output_tok = await provider.generate(
        system_prompt="sys",
        human_message="user",
        model="gpt-4o-mini",
        max_tokens=100,
        temperature=0.2,
        output_format="json",
    )

    assert raw == '{"ok": true}'
    assert input_tok == 11
    assert output_tok == 7


@pytest.mark.asyncio
async def test_generate_raises_when_no_choices() -> None:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[], usage=None))
    provider = OpenAIProvider(api_key="test-key")
    provider._client = client

    with pytest.raises(ValueError, match="no completion choices"):
        await provider.generate(
            system_prompt="sys",
            human_message="user",
            model="gpt-4o-mini",
            max_tokens=100,
            temperature=0.2,
        )


@pytest.mark.asyncio
async def test_generate_json_array_skips_json_object_response_format() -> None:
    client = MagicMock()
    choice = SimpleNamespace(message=SimpleNamespace(content='[{"title": "cand"}]'))
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=3)
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[choice], usage=usage))
    provider = OpenAIProvider(api_key="test-key")
    provider._client = client

    raw, _, _ = await provider.generate(
        system_prompt="sys",
        human_message="return json array",
        model="gpt-4o-mini",
        max_tokens=100,
        temperature=0.2,
        output_format="json",
        json_root="any",
    )

    assert raw == '[{"title": "cand"}]'
    kwargs = client.chat.completions.create.await_args.kwargs
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_generate_json_object_uses_response_format() -> None:
    client = MagicMock()
    choice = SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=3)
    client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(choices=[choice], usage=usage))
    provider = OpenAIProvider(api_key="test-key")
    provider._client = client

    await provider.generate(
        system_prompt="sys",
        human_message="user",
        model="gpt-4o-mini",
        max_tokens=100,
        temperature=0.2,
        output_format="json",
        json_root="object",
    )

    kwargs = client.chat.completions.create.await_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_aclose_calls_underlying_client() -> None:
    provider = OpenAIProvider(api_key="test-key")
    provider._client.aclose = AsyncMock()
    await provider.aclose()
    provider._client.aclose.assert_awaited_once()
