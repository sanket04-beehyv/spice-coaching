"""Embedding service — delegates to ai-runtime /internal/embed.

Platform keeps no direct AI provider credentials. All embedding calls route
through ai-runtime, which owns the Google/OpenAI API key.
"""

from __future__ import annotations

import logging

from platform_service.deps import get_ai_client
from platform_service.integrations.ai_runtime_client import AIRuntimeClient

logger = logging.getLogger(__name__)


class EmbeddingsService:
    def __init__(self, client: AIRuntimeClient | None = None) -> None:
        self._client = client or get_ai_client()

    async def embed_query(self, text: str) -> list[float]:
        """Return embedding vector for a single query string."""
        results = await self._client.embed([text])
        return results[0]

    async def embed_scenarios(self, texts: list[tuple[str, list[str]]]) -> list[list[float]]:
        """Embed scenario content for storage.

        Args:
            texts: list of (sop_excerpt_en, key_points) tuples.

        Returns:
            list of embedding vectors, one per input.
        """
        combined = [f"{excerpt} {' '.join(kp)}" if kp else excerpt for excerpt, kp in texts]
        return await self._client.embed(combined)
