"""Log raw LLM provider output for debugging."""

from __future__ import annotations

import logging
from typing import Literal

from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import AiProvider

logger = logging.getLogger(__name__)

LlmLogReason = Literal["parse_failure", "debug"]


def _truncate_for_log(raw_text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(raw_text) <= max_chars:
        return raw_text
    return f"{raw_text[:max_chars]}... [truncated, total_len={len(raw_text)}]"


def log_llm_raw_text(
    *,
    request_id: str,
    generation_type: GenerationType,
    provider: AiProvider,
    model: str,
    raw_text: str,
    reason: LlmLogReason,
    max_chars: int,
) -> None:
    """Emit a structured log line with the raw provider output."""
    body = _truncate_for_log(raw_text, max_chars)
    msg = "LLM response request_id=%s type=%s provider=%s model=%s reason=%s raw_text=%s"
    args = (request_id, generation_type.value, provider, model, reason, body)
    if reason == "parse_failure":
        logger.warning(msg, *args)
    else:
        logger.info(msg, *args)
