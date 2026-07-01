"""Abstract AI provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal


class ProviderImage:
    """In-memory image payload passed from PromptExecutor to a provider.

    The HTTP boundary uses base64 (mc_contracts.InferenceImage); inside
    ai-runtime we decode once and pass raw bytes to the provider.
    """

    __slots__ = ("data", "mime_type", "label")

    def __init__(self, data: bytes, mime_type: str, label: str | None = None) -> None:
        self.data = data
        self.mime_type = mime_type
        self.label = label


class BaseProvider(ABC):
    """Every AI provider must implement generate(), embed(), and media transcription."""

    @abstractmethod
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
        """Execute a prompt and return (raw_text, input_tokens, output_tokens).

        `images` is optional. Providers that don't support multimodal input
        should raise NotImplementedError when called with non-empty images.
        """
        ...

    @abstractmethod
    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        """Return embedding vectors for a list of texts."""
        ...

    @abstractmethod
    async def transcribe_media(self, media_bytes: bytes, mime_type: str, model: str) -> str:
        """Return speech transcript text from audio/video bytes."""
        ...

    async def aclose(self) -> None:
        """Release underlying SDK HTTP resources (override in concrete providers)."""
        return None
