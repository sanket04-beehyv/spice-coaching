"""Shared internal API authentication helpers."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from ai_runtime.config import get_settings


def require_internal_token(request: Request) -> None:
    """Validate platform -> ai-runtime shared-secret token."""
    settings = get_settings()
    token = request.headers.get("X-Internal-Token", "")
    if not hmac.compare_digest(token, settings.internal_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal token",
        )
