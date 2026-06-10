"""Internal embed endpoint — platform → ai-runtime.

POST /internal/embed
Body: {"texts": ["...", "..."]}
Returns: {"embeddings": [[0.1, 0.2, ...], ...]}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ai_runtime.security import require_internal_token
from ai_runtime.services.prompt_executor import PromptExecutor

router = APIRouter(prefix="/internal", tags=["internal"])
logger = logging.getLogger(__name__)

_executor = PromptExecutor()


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]


@router.post("/embed", response_model=EmbedResponse)
async def embed(body: EmbedRequest, _: None = Depends(require_internal_token)) -> EmbedResponse:
    """Return embedding vectors for a list of texts."""
    if not body.texts:
        return EmbedResponse(embeddings=[])
    if len(body.texts) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 texts per request")
    embeddings = await _executor.embed(body.texts)
    return EmbedResponse(embeddings=embeddings)
