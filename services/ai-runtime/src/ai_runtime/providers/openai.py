"""OpenAI provider adapter."""

from __future__ import annotations

import base64
import logging
from typing import Any, Literal

from openai import AsyncOpenAI

from ai_runtime.providers.base import BaseProvider, ProviderImage

logger = logging.getLogger(__name__)

_EXT_BY_TRANSCRIPTION_MIME_TYPE = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
}


class OpenAIProvider(BaseProvider):
    """Adapter for OpenAI API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        organization: str | None = None,
        embedding_dimension: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._embedding_dimension = embedding_dimension
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            timeout=timeout_seconds,
        )

    async def generate(
        self,
        system_prompt: str,
        human_message: str,
        model: str,
        max_tokens: int,
        temperature: float,
        images: list[ProviderImage] | None = None,
        output_format: str = "json",
        json_root: Literal["object", "any"] = "object",
    ) -> tuple[str, int, int]:
        """Call OpenAI and return (raw_text, input_tokens, output_tokens)."""
        # OpenAI JSON mode requires the word 'json' in the prompt.
        if output_format == "json":
            if "json" not in system_prompt.lower() and "json" not in human_message.lower():
                system_prompt += "\n\nIMPORTANT: You must return the response in valid JSON format."

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]

        human_content: list[dict[str, Any]] = [{"type": "text", "text": human_message}]
        for img in images or []:
            b64_data = base64.b64encode(img.data).decode("utf-8")
            human_content.append(
                {"type": "image_url", "image_url": {"url": f"data:{img.mime_type};base64,{b64_data}"}}
            )

        messages.append({"role": "user", "content": human_content})

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if output_format == "json" and json_root == "object":
            kwargs["response_format"] = {"type": "json_object"}

        response = await self._client.chat.completions.create(**kwargs)

        if not response.choices:
            raise ValueError("OpenAI returned no completion choices")
        choice = response.choices[0]
        raw_text = choice.message.content or ""

        input_tokens = response.usage.prompt_tokens if response.usage else 0
        output_tokens = response.usage.completion_tokens if response.usage else 0

        return raw_text, input_tokens, output_tokens

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """Return embedding vectors for the given texts.

        ``dimensions=`` is only honoured by ``text-embedding-3*`` models;
        passing it to older ``ada-002`` returns a 400. The prefix check
        gates the parameter so the provider works against both families
        from one code path.
        """
        kwargs: dict[str, Any] = {"model": model, "input": texts}
        if self._embedding_dimension is not None and model.startswith("text-embedding-3"):
            kwargs["dimensions"] = self._embedding_dimension
        response = await self._client.embeddings.create(**kwargs)
        # Sort by index to ensure order matches input
        data = sorted(response.data, key=lambda x: x.index)
        return [item.embedding for item in data]

    async def transcribe_media(self, media_bytes: bytes, mime_type: str, model: str) -> str:
        """Transcribe audio/video bytes using OpenAI transcription API."""
        if not media_bytes:
            raise ValueError("media payload is empty")

        normalised_mime_type = mime_type.lower()
        ext = _EXT_BY_TRANSCRIPTION_MIME_TYPE.get(normalised_mime_type)
        if ext is None:
            raise ValueError(f"unsupported transcription mime_type: {mime_type!r}")

        transcript = await self._client.audio.transcriptions.create(
            model=model,
            file=(f"upload{ext}", media_bytes, normalised_mime_type),
        )
        return (getattr(transcript, "text", "") or "").strip()
