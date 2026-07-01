"""Middleware that validates SPICE JWTs via auth-service ``/authenticate``."""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from platform_service.config import get_settings
from platform_service.deps import get_spice_auth_client
from platform_service.integrations.spice_auth_client import SpiceAuthClient, SpiceAuthError

logger = logging.getLogger(__name__)


class SpiceAuthMiddleware(BaseHTTPMiddleware):
    """When ``spice_auth_enabled``, validate every non-exempt request."""

    def __init__(self, app, client: SpiceAuthClient | None = None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(app)
        self._client = client

    def _get_client(self) -> SpiceAuthClient:
        if self._client is not None:
            return self._client
        return get_spice_auth_client()

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        settings = get_settings()
        if not settings.spice_auth_enabled:
            return await call_next(request)

        if request.url.path in settings.spice_auth_exempt_path_set:
            return await call_next(request)

        try:
            authorization = request.headers.get("authorization")
            SpiceAuthClient.validate_authorization_header(authorization)
            contexts = await self._get_client().authenticate(
                authorization=authorization,  # type: ignore[arg-type]
                client=request.headers.get("client"),
                auth_cookie=request.headers.get("auth-cookie"),
            )
        except SpiceAuthError as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

        request.state.spice_contexts = contexts
        request.state.spice_user = contexts.user_detail
        return await call_next(request)
