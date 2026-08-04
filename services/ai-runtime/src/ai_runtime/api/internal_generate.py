"""Internal generate endpoints — platform → ai-runtime.

POST /internal/generate/{generation_type}

All ``GenerationType`` values share the same handler. The path param is
validated against the enum and must match the request body.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from mc_contracts.enums import GenerationType
from mc_contracts.errors import ErrorCode
from mc_contracts.internal_ai import InferenceRequest, InferenceResponse
from mc_foundation.problem import AppError

from ai_runtime.config import get_settings
from ai_runtime.security import require_internal_token
from ai_runtime.services.prompt_executor import PromptExecutor

router = APIRouter(prefix="/internal", tags=["internal"])
logger = logging.getLogger(__name__)

_executor = PromptExecutor()


@router.post("/generate/{generation_type}", response_model=InferenceResponse)
async def generate(
    generation_type: str,
    body: InferenceRequest,
    _: None = Depends(require_internal_token),
) -> InferenceResponse:
    """Execute a fully-resolved InferenceRequest and return InferenceResponse."""
    try:
        gt = GenerationType(generation_type)
    except ValueError:
        raise AppError(
            ErrorCode.BAD_REQUEST.value,
            f"Unknown generation_type '{generation_type}'. Valid: {[e.value for e in GenerationType]}",
            status=400,
        ) from None

    if body.generation_type != gt:
        raise AppError(
            ErrorCode.BAD_REQUEST.value,
            f"Path generation_type '{generation_type}' does not match body '{body.generation_type.value}'",
            status=400,
        )

    logger.info(
        "generate request_id=%s type=%s provider=%s",
        body.request_id,
        gt.value,
        get_settings().ai_provider,
    )
    return await _executor.execute(body)
