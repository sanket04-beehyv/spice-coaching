"""Resolve ai-runtime InferenceResponse payloads on the platform side."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeVar

from mc_contracts.internal_ai import InferenceResponse

from platform_service.services.llm_text_utils import strip_json_fence

T = TypeVar("T")


def resolve_parsed_json(
    response: InferenceResponse,
    *,
    fallback_text: str | None = None,
) -> Any:
    """Return parsed_json when present, otherwise parse raw_text."""
    if response.parsed_json is not None:
        return response.parsed_json
    text = fallback_text if fallback_text is not None else response.raw_text
    return json.loads(strip_json_fence(text or ""))


def resolve_parsed_dict(
    response: InferenceResponse,
    *,
    on_error: Callable[[json.JSONDecodeError], T] | None = None,
    fallback_text: str | None = None,
) -> dict[str, Any] | T:
    """Resolve a dict-shaped LLM payload, with optional decode-error handler."""
    try:
        payload = resolve_parsed_json(response, fallback_text=fallback_text)
    except json.JSONDecodeError as exc:
        if on_error is not None:
            return on_error(exc)
        raise
    if not isinstance(payload, dict):
        raise TypeError(f"LLM output must be a JSON object, got {type(payload).__name__}")
    return payload
