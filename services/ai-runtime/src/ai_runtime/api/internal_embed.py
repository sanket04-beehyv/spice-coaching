"""Internal embed endpoint — platform → ai-runtime.

POST /internal/embed
Body: EmbedRequest
Returns: EmbedResponse
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from mc_contracts.errors import ErrorCode
from mc_contracts.internal_ai import EmbedRequest, EmbedResponse
from mc_foundation.problem import AppError

from ai_runtime.security import require_internal_token
from ai_runtime.services.prompt_executor import PromptExecutor

router = APIRouter(prefix="/internal", tags=["internal"])
logger = logging.getLogger(__name__)

_executor = PromptExecutor()


@router.post("/embed", response_model=EmbedResponse)
async def embed(body: EmbedRequest, _: None = Depends(require_internal_token)) -> EmbedResponse:
    """Return embedding vectors for a list of texts."""
    if not body.texts:
        return EmbedResponse(embeddings=[])
    if len(body.texts) > 100:
        raise AppError(ErrorCode.BAD_REQUEST.value, "Maximum 100 texts per request", status=400)
    embeddings = await _executor.embed(body.texts)
    return EmbedResponse(embeddings=embeddings)
