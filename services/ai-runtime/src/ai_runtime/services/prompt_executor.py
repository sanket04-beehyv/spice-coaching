"""Prompt executor — selects provider and runs inference.

ai-runtime owns provider selection for all capabilities (generate, embed,
transcribe) via ``Settings.ai_provider``. Model id and generation budgets
(max_tokens, temperature) are resolved from per-``GenerationType`` profiles
in ``ai_runtime.generation_profiles``; platform sends only the role plus
prompt/content constraints.

v3.3 additions:
- Decodes optional `image_attachments` (base64) from InferenceRequest into
  `ProviderImage` (raw bytes) and forwards to the provider for multimodal
  calls (VISION_EXTRACTION).
- Generation types share the same provider call path; platform owns the
  resolved prompt and structured output expectations.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import InferenceImage, InferenceRequest, InferenceResponse, TokenUsage

from ai_runtime.config import get_settings
from ai_runtime.generation_profiles import resolve_profile
from ai_runtime.providers.base import BaseProvider, ProviderImage
from ai_runtime.providers.google import GoogleProvider
from ai_runtime.services.embedding_vector import align_embedding_dimension
from ai_runtime.services.llm_response_logging import log_llm_raw_text
from ai_runtime.services.response_parser import extract_json
from ai_runtime.services.transient_errors import is_transient_provider_error

logger = logging.getLogger(__name__)


# Generation types whose JSON output may be a top-level array.
_JSON_ARRAY_GENERATION_TYPES = frozenset(
    {
        GenerationType.MODULE_IDENTIFICATION,
        GenerationType.DISTRACTOR_CRITIQUE,
    }
)

# Backoffs span past the typical 60s per-minute quota refill window. With
# the previous (2.0, 5.0, 10.0) totalling 17s, retries were guaranteed to
# hit the same exhausted bucket. (10.0, 30.0, 60.0) → 100s total, with
# the longest delay alone covering the worst-case quota window.
_RETRY_BACKOFFS_S = (10.0, 30.0, 60.0)


def _is_transient(exc: Exception) -> bool:
    """Backward-compatible alias for tests and legacy imports."""
    return is_transient_provider_error(exc)


def _json_root_for_generation(generation_type: GenerationType) -> str:
    if generation_type in _JSON_ARRAY_GENERATION_TYPES:
        return "any"
    return "object"


async def _call_with_transient_retry(
    *,
    log_context: str,
    provider_name: str,
    model: str,
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    """Retry ``operation`` on transient provider errors with shared backoff."""
    last_exc: Exception | None = None
    for attempt in range(len(_RETRY_BACKOFFS_S) + 1):
        try:
            return await operation()
        except Exception as exc:
            last_exc = exc
            transient = _is_transient(exc)
            final = attempt >= len(_RETRY_BACKOFFS_S)
            if not transient or final:
                logger.exception(
                    "Provider error context=%s provider=%s model=%s attempt=%d transient=%s",
                    log_context,
                    provider_name,
                    model,
                    attempt + 1,
                    transient,
                )
                break
            wait = _RETRY_BACKOFFS_S[attempt]
            logger.warning(
                "Transient provider error context=%s attempt=%d/%d retrying in %.1fs: %s",
                log_context,
                attempt + 1,
                len(_RETRY_BACKOFFS_S) + 1,
                wait,
                exc,
            )
            await asyncio.sleep(wait)
    assert last_exc is not None
    raise last_exc


_provider_cache: dict[str, BaseProvider] = {}


def _build_provider(provider_name: str) -> BaseProvider:
    settings = get_settings()
    timeout_seconds = settings.provider_timeout_seconds
    if provider_name == "google":
        service_account_info = None
        if settings.google_service_account_base64:
            try:
                decoded = base64.b64decode(settings.google_service_account_base64).decode("utf-8")
                service_account_info = json.loads(decoded)
            except Exception as exc:
                logger.error("Failed to decode google_service_account_base64: %s", exc)
                # Fall through to other auth paths; GoogleProvider will raise
                # a clear error if no usable credentials remain.
        if service_account_info or settings.google_use_vertex:
            return GoogleProvider(
                use_vertex=True,
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
                service_account_info=service_account_info,
                embedding_dimension=settings.google_embedding_dimension,
                timeout_seconds=timeout_seconds,
            )
        return GoogleProvider(
            api_key=settings.google_api_key,
            embedding_dimension=settings.google_embedding_dimension,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"Unsupported provider: {provider_name}")


def _get_provider(provider_name: str) -> BaseProvider:
    cached = _provider_cache.get(provider_name)
    if cached is not None:
        return cached
    provider = _build_provider(provider_name)
    _provider_cache[provider_name] = provider
    return provider


def clear_provider_cache() -> None:
    """Drop cached provider clients (call on application shutdown)."""
    _provider_cache.clear()


async def close_providers() -> None:
    """Close underlying SDK clients and drop the provider cache."""
    for provider in list(_provider_cache.values()):
        await provider.aclose()
    _provider_cache.clear()


def _decode_image_attachments(attachments: list[InferenceImage]) -> list[ProviderImage]:
    """Decode base64 image attachments to raw bytes for the provider call.

    Raises ValueError on malformed base64 (caller catches and returns an
    InferenceResponse with `error` populated).
    """
    decoded: list[ProviderImage] = []
    for idx, att in enumerate(attachments):
        try:
            data = base64.b64decode(att.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"image_attachments[{idx}] (label={att.label!r}) is not valid base64: {exc}"
            ) from exc
        decoded.append(ProviderImage(data=data, mime_type=att.mime_type, label=att.label))
    return decoded


class PromptExecutor:
    """Executes an InferenceRequest against the appropriate AI provider."""

    def __init__(self) -> None:
        self._settings = get_settings()

    async def execute(self, request: InferenceRequest) -> InferenceResponse:
        settings = self._settings
        provider_name = settings.ai_provider
        profile = resolve_profile(request.generation_type, settings)
        model = profile.model
        max_tokens = profile.max_tokens
        temperature = profile.temperature

        # Decode image attachments before timing the provider call so that
        # base64 errors are surfaced as a clean InferenceResponse.
        try:
            provider_images = _decode_image_attachments(request.image_attachments)
        except ValueError as exc:
            logger.error(
                "Image attachment decode failed request_id=%s: %s",
                request.request_id,
                exc,
            )
            return InferenceResponse(
                request_id=request.request_id,
                generation_type=request.generation_type,
                provider=provider_name,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                raw_text="",
                parsed_json=None,
                latency_ms=0,
                error=str(exc),
            )

        provider = _get_provider(provider_name)
        start_ms = time.monotonic()

        try:
            raw_text, input_tokens, output_tokens = await _call_with_transient_retry(
                log_context=f"generate request_id={request.request_id}",
                provider_name=provider_name,
                model=model,
                operation=lambda: provider.generate(
                    system_prompt=request.prompt.resolved_system_prompt,
                    human_message=request.prompt.resolved_human_message,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    images=provider_images or None,
                    output_format=request.constraints.output_format,
                    json_root=_json_root_for_generation(request.generation_type),
                ),
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - start_ms) * 1000)
            return InferenceResponse(
                request_id=request.request_id,
                generation_type=request.generation_type,
                provider=provider_name,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                raw_text="",
                parsed_json=None,
                latency_ms=latency_ms,
                error=str(exc),
            )

        latency_ms = int((time.monotonic() - start_ms) * 1000)
        error = None

        if settings.log_llm_responses:
            log_llm_raw_text(
                request_id=request.request_id,
                generation_type=request.generation_type,
                provider=provider_name,
                model=model,
                raw_text=raw_text,
                reason="debug",
                max_chars=settings.log_llm_response_max_chars,
            )

        parsed_json = None
        if request.constraints.output_format == "json":
            parsed_json = extract_json(raw_text)
            if parsed_json is None and not error:
                # Retry once on JSON parse failure
                if settings.json_parse_retries > 0:
                    logger.info("JSON parse failed, retrying request_id=%s", request.request_id)
                    try:
                        raw_text2, it2, ot2 = await _call_with_transient_retry(
                            log_context=f"json_parse_retry request_id={request.request_id}",
                            provider_name=provider_name,
                            model=model,
                            operation=lambda: provider.generate(
                                system_prompt=request.prompt.resolved_system_prompt,
                                human_message=request.prompt.resolved_human_message,
                                model=model,
                                max_tokens=max_tokens,
                                temperature=temperature,
                                images=provider_images or None,
                                output_format=request.constraints.output_format,
                            ),
                        )
                        parsed_json = extract_json(raw_text2)
                        if parsed_json is not None:
                            raw_text = raw_text2
                            input_tokens += it2
                            output_tokens += ot2
                    except Exception:
                        logger.warning("Retry also failed request_id=%s", request.request_id)

        if request.constraints.output_format == "json" and parsed_json is None:
            error = "failed to parse JSON from provider output"
            log_llm_raw_text(
                request_id=request.request_id,
                generation_type=request.generation_type,
                provider=provider_name,
                model=model,
                raw_text=raw_text,
                reason="parse_failure",
                max_chars=settings.log_llm_response_max_chars,
            )

        # parsed_json may be a dict OR a list (top-level JSON arrays are valid
        # for module_identification, distractor_critique, etc.). Both are
        # surfaced via the same field; downstream typing on the response
        # accepts either by widening the field annotation in mc_contracts.
        return InferenceResponse(
            request_id=request.request_id,
            generation_type=request.generation_type,
            provider=provider_name,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            raw_text=raw_text,
            parsed_json=parsed_json,
            latency_ms=latency_ms,
            token_usage=TokenUsage(input=input_tokens, output=output_tokens),
            error=error,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings aligned to the configured corpus dimension.

        Alignment runs once here — platform-side helpers assert against
        the same dimension rather than truncating again, so a misconfigured
        provider surfaces as an ``EmbeddingDimensionError`` instead of a
        silent double truncation.
        """
        settings = self._settings
        provider = _get_provider(settings.ai_provider)
        model = settings.google_embedding_model

        vectors = await _call_with_transient_retry(
            log_context="embed",
            provider_name=settings.ai_provider,
            model=model,
            operation=lambda: provider.embed(texts, model=model),
        )
        expected = settings.embedding_dimension
        return [align_embedding_dimension(v, expected_dim=expected) for v in vectors]

    async def transcribe_media(self, media_bytes: bytes, mime_type: str) -> str:
        """Return transcript text for audio/video bytes using configured provider."""
        settings = self._settings
        provider = _get_provider(settings.ai_provider)
        model = settings.google_transcription_model
        return await _call_with_transient_retry(
            log_context="transcribe",
            provider_name=settings.ai_provider,
            model=model,
            operation=lambda: provider.transcribe_media(
                media_bytes=media_bytes,
                mime_type=mime_type,
                model=model,
            ),
        )
