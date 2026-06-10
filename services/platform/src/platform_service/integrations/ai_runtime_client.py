"""HTTP client for the internal ai-runtime service.

Platform assembles fully-resolved InferenceRequest objects and posts them to
ai-runtime. AI runtime owns all LLM provider adapters; platform owns all state.
"""

from __future__ import annotations

import base64
import logging

import httpx
from mc_contracts.internal_ai import (
    InferenceRequest,
    InferenceResponse,
    TranscribeRequest,
    TranscribeResponse,
)

from platform_service.config import get_settings

logger = logging.getLogger(__name__)


class AIRuntimeClient:
    """Thin httpx client for ai-runtime internal API.

    Holds one ``httpx.AsyncClient`` per instance for connection reuse. The
    process-level singleton in ``deps.py`` should be closed on app shutdown.
    """

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
        transcribe_timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.ai_runtime_base_url).rstrip("/")
        self._token = token or settings.ai_runtime_token.get_secret_value()
        self._timeout = timeout or settings.ai_runtime_timeout_seconds
        self._transcribe_timeout = transcribe_timeout or settings.ai_runtime_transcribe_timeout_seconds
        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._transcribe_client = httpx.AsyncClient(timeout=self._transcribe_timeout)

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._transcribe_client.aclose()

    async def generate(self, request: InferenceRequest) -> InferenceResponse:
        """Post a fully-resolved InferenceRequest to ai-runtime and return the response."""
        url = f"{self._base_url}/internal/generate/{request.generation_type.value}"
        headers = {"X-Internal-Token": self._token, "Content-Type": "application/json"}
        payload = request.model_dump(mode="json")

        try:
            resp = await self._client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return InferenceResponse.model_validate(resp.json())
        except httpx.HTTPStatusError as exc:
            logger.error(
                "ai-runtime returned %s for generation_type=%s request_id=%s: %s",
                exc.response.status_code,
                request.generation_type.value,
                request.request_id,
                exc.response.text[:200],
            )
            raise
        except httpx.RequestError as exc:
            logger.error(
                "ai-runtime unreachable generation_type=%s request_id=%s: %s",
                request.generation_type.value,
                request.request_id,
                exc,
            )
            raise

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Request text embeddings from ai-runtime.

        Returns a list of embedding vectors (one per input text), in the same order.
        """
        url = f"{self._base_url}/internal/embed"
        headers = {"X-Internal-Token": self._token, "Content-Type": "application/json"}

        resp = await self._client.post(url, json={"texts": texts}, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["embeddings"]

    async def transcribe_media(self, media_bytes: bytes, mime_type: str) -> str:
        """Request speech transcription from ai-runtime."""
        url = f"{self._base_url}/internal/transcribe"
        headers = {"X-Internal-Token": self._token, "Content-Type": "application/json"}
        payload = TranscribeRequest(
            data_base64=base64.b64encode(media_bytes).decode("utf-8"),
            mime_type=mime_type,
        )
        resp = await self._transcribe_client.post(url, json=payload.model_dump(mode="json"), headers=headers)
        resp.raise_for_status()
        return TranscribeResponse.model_validate(resp.json()).text.strip()
