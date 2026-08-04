"""FastAPI dependency to read the authenticated SPICE user from request state."""

from __future__ import annotations

from fastapi import Request
from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError

from platform_service.auth.spice_context import SpiceUserContext
from platform_service.config import get_settings


def get_spice_user(request: Request) -> SpiceUserContext:
    """Return the user context set by :class:`SpiceAuthMiddleware`."""
    user = getattr(request.state, "spice_user", None)
    if user is None:
        raise AppError(ErrorCode.NOT_AUTHENTICATED.value, "not authenticated", status=401)
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
            raise AppError(ErrorCode.NOT_AUTHENTICATED.value, "not authenticated", status=401)
        return "admin"
    if user.username:
        return user.username
    if user.id is not None:
        return str(user.id)
    return "admin"
