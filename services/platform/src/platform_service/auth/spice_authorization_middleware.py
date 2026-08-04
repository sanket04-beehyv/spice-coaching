"""Middleware enforcing admin vs device API planes from SPICE role suite access."""

from __future__ import annotations

import logging

from mc_contracts.errors import ErrorCode
from mc_foundation.problem import problem_json_response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from platform_service.auth.spice_context import SpiceUserContext
from platform_service.auth.spice_principal import is_admin_principal, is_device_principal
from platform_service.config import get_settings

logger = logging.getLogger(__name__)

FORBIDDEN_DETAIL = "insufficient role for this API"


def _relative_path(request_path: str, api_root: str) -> str:
    """Path under ``api_root`` without leading slash (e.g. ``admin/modules``)."""
    root = api_root.rstrip("/")
    if request_path.startswith(root):
        return request_path[len(root) :].lstrip("/")
    return request_path.lstrip("/")


def _matches_prefix(relative: str, prefix: str) -> bool:
    rel = relative.lower()
    pfx = prefix.lower()
    return rel == pfx or rel.startswith(f"{pfx}/")


def _plane_for_path(
    relative: str,
    admin_prefixes: frozenset[str],
    device_prefixes: frozenset[str],
) -> str | None:
    """Return ``admin``, ``device``, or None if the path is not gated."""
    admin_match = any(_matches_prefix(relative, p) for p in admin_prefixes)
    device_match = any(_matches_prefix(relative, p) for p in device_prefixes)
    if admin_match and device_match:
        logger.warning("path matches both admin and device planes relative=%s", relative)
        return "admin"
    if admin_match:
        return "admin"
    if device_match:
        return "device"
    return None


class SpiceAuthorizationMiddleware(BaseHTTPMiddleware):
    """When SPICE auth is enabled, restrict paths to admin or device principals."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        settings = get_settings()
        if not settings.spice_auth_enabled:
            return await call_next(request)

        if request.url.path in settings.spice_auth_exempt_path_set:
            return await call_next(request)

        user: SpiceUserContext | None = getattr(request.state, "spice_user", None)
        if user is None:
            return await call_next(request)

        relative = _relative_path(request.url.path, settings.api_root_path_normalized)
        plane = _plane_for_path(
            relative,
            settings.spice_admin_path_prefix_set,
            settings.spice_device_path_prefix_set,
        )
        if plane is None:
            return await call_next(request)

        if plane == "admin" and not is_admin_principal(user):
            logger.info(
                "spice authorization denied plane=admin path=%s user_id=%s",
                request.url.path,
                user.id,
            )
            return problem_json_response(
                code=ErrorCode.FORBIDDEN.value,
                detail=FORBIDDEN_DETAIL,
                status=403,
                instance=str(request.url.path),
            )

        if plane == "device" and not is_device_principal(user):
            logger.info(
                "spice authorization denied plane=device path=%s user_id=%s",
                request.url.path,
                user.id,
            )
            return problem_json_response(
                code=ErrorCode.FORBIDDEN.value,
                detail=FORBIDDEN_DETAIL,
                status=403,
                instance=str(request.url.path),
            )

        return await call_next(request)
