from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ai_runtime.api import internal_transcribe
from ai_runtime.api.internal_transcribe import transcribe
from ai_runtime.services.prompt_executor import PromptExecutor
from fastapi import HTTPException
from mc_contracts.internal_ai import TranscribeRequest


@pytest.mark.asyncio
async def test_transcribe_media_uses_provider_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = AsyncMock()
    provider.transcribe_media = AsyncMock(return_value="hello")
    monkeypatch.setattr(
        "ai_runtime.services.prompt_executor._get_provider",
        lambda _name: provider,
    )

    executor = PromptExecutor()
    monkeypatch.setattr(
        executor,
        "_settings",
        SimpleNamespace(
            ai_provider="openai",
            openai_transcription_model="gpt-4o-mini-transcribe",
            google_transcription_model="gemini-2.5-flash",
        ),
    )

    text = await executor.transcribe_media(b"media-bytes", "audio/mpeg")

    assert text == "hello"
    provider.transcribe_media.assert_awaited_once_with(
        media_bytes=b"media-bytes",
        mime_type="audio/mpeg",
        model="gpt-4o-mini-transcribe",
    )


@pytest.mark.asyncio
async def test_transcribe_endpoint_rejects_unsupported_mime_type() -> None:
    body = TranscribeRequest(data_base64="ZmFrZQ==", mime_type="application/octet-stream")

    with pytest.raises(HTTPException) as exc_info:
        await transcribe(body, None)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_transcribe_endpoint_rejects_payload_above_provider_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = TranscribeRequest(data_base64="ZmFrZQ==", mime_type="audio/mpeg")
    monkeypatch.setattr(internal_transcribe, "_provider_media_limit_bytes", lambda _provider: 3)

    with pytest.raises(HTTPException) as exc_info:
        await transcribe(body, None)

    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_transcribe_endpoint_rejects_empty_provider_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = TranscribeRequest(data_base64="ZmFrZQ==", mime_type="audio/mpeg")
    monkeypatch.setattr(internal_transcribe._executor, "transcribe_media", AsyncMock(return_value=" "))

    with pytest.raises(HTTPException) as exc_info:
        await transcribe(body, None)

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_transcribe_endpoint_maps_provider_quota_error_to_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = TranscribeRequest(data_base64="ZmFrZQ==", mime_type="audio/mpeg")
    monkeypatch.setattr(
        internal_transcribe._executor,
        "transcribe_media",
        AsyncMock(side_effect=RuntimeError("429 insufficient_quota")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await transcribe(body, None)

    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_transcribe_endpoint_maps_generic_provider_error_to_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = TranscribeRequest(data_base64="ZmFrZQ==", mime_type="audio/mpeg")
    monkeypatch.setattr(
        internal_transcribe._executor,
        "transcribe_media",
        AsyncMock(side_effect=RuntimeError("upstream connection reset")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await transcribe(body, None)

    assert exc_info.value.status_code == 502
