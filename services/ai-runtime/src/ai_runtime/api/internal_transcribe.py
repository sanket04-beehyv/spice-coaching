"""Internal transcription endpoint — platform → ai-runtime.

POST /internal/transcribe
Body: {"data_base64": "...", "mime_type": "audio/mpeg"}
Returns: {"text": "..."}
"""

from __future__ import annotations

import base64
import binascii
import logging

from fastapi import APIRouter, Depends, HTTPException
from mc_contracts.internal_ai import (
    GEMINI_INLINE_TRANSCRIPTION_MAX_BYTES,
    TranscribeRequest,
    TranscribeResponse,
)

from ai_runtime.config import get_settings
from ai_runtime.security import require_internal_token
from ai_runtime.services.prompt_executor import PromptExecutor

router = APIRouter(prefix="/internal", tags=["internal"])
logger = logging.getLogger(__name__)
_executor = PromptExecutor()

_SUPPORTED_TRANSCRIPTION_MIME_TYPES = frozenset(
    {
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/mp4",
        "audio/m4a",
        "audio/flac",
        "audio/ogg",
        "audio/webm",
        "video/mp4",
        "video/webm",
        "video/quicktime",
        "video/x-matroska",
    }
)


def _provider_media_limit_bytes(provider: str) -> int:
    _ = provider
    return GEMINI_INLINE_TRANSCRIPTION_MAX_BYTES


def _provider_error_status(exc: Exception) -> int:
    message = str(exc).lower()
    if "429" in message or "rate limit" in message or "insufficient_quota" in message:
        return 429
    return 502


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(
    body: TranscribeRequest,
    _: None = Depends(require_internal_token),
) -> TranscribeResponse:
    """Return transcript text extracted from audio/video bytes."""
    mime_type = body.mime_type.lower()
    if mime_type not in _SUPPORTED_TRANSCRIPTION_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"unsupported transcription mime_type {body.mime_type!r}")
    try:
        media_bytes = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"invalid data_base64: {exc}") from exc
    if not media_bytes:
        raise HTTPException(status_code=400, detail="empty media payload")
    max_bytes = _provider_media_limit_bytes(get_settings().ai_provider)
    if len(media_bytes) > max_bytes:
        raise HTTPException(status_code=413, detail=f"media payload exceeds {max_bytes} bytes")
    try:
        text = await _executor.transcribe_media(media_bytes=media_bytes, mime_type=mime_type)
    except Exception as exc:
        status_code = _provider_error_status(exc)
        logger.exception("Transcription provider failed status=%d mime_type=%s", status_code, mime_type)
        detail = (
            "transcription provider rate limited or quota exhausted"
            if status_code == 429
            else "transcription provider failed"
        )
        raise HTTPException(status_code=status_code, detail=detail) from exc
    if not text.strip():
        raise HTTPException(status_code=422, detail="provider returned empty transcript")
    return TranscribeResponse(text=text)
