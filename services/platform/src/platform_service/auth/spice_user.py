"""FastAPI dependency to read the authenticated SPICE user from request state."""

from __future__ import annotations

from fastapi import HTTPException, Request

from platform_service.auth.spice_context import SpiceUserContext
from platform_service.config import get_settings


def get_spice_user(request: Request) -> SpiceUserContext:
    """Return the user context set by :class:`SpiceAuthMiddleware`."""
    user = getattr(request.state, "spice_user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def resolve_spice_actor(request: Request) -> str:
    """Return an audit actor from the authenticated SPICE user.

    When SPICE auth is enabled, unauthenticated requests raise 401. When auth
    is disabled (local dev), fall back to ``admin``.
    """
    settings = get_settings()
    user = getattr(request.state, "spice_user", None)
    if user is None:
        if settings.spice_auth_enabled:
            raise HTTPException(status_code=401, detail="not authenticated")
        return "admin"
    if user.username:
        return user.username
    if user.id is not None:
        return str(user.id)
    return "admin"
