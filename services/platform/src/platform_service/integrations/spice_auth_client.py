"""HTTP client for SPICE auth-service token validation."""

from __future__ import annotations

import logging

import httpx

from platform_service.auth.spice_context import SpiceContexts
from platform_service.config import get_settings

logger = logging.getLogger(__name__)

BEARER_PREFIX = "Bearer "


class SpiceAuthError(Exception):
    """Raised when token validation fails or auth-service is unavailable."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class SpiceAuthClient:
    """Calls auth-service ``POST /authenticate`` with forwarded caller headers."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        default_client: str | None = None,
    ) -> None:
        settings = get_settings()
        self._authenticate_url = f"{(base_url or settings.spice_auth_base_url).rstrip('/')}/authenticate"
        self._timeout = timeout if timeout is not None else settings.spice_auth_timeout_seconds
        self._default_client = default_client or settings.spice_auth_default_client
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def validate_authorization_header(authorization: str | None) -> str:
        if not authorization or not authorization.startswith(BEARER_PREFIX):
            raise SpiceAuthError(401, "missing or invalid Authorization header")
        token = authorization[len(BEARER_PREFIX) :].strip()
        if not token:
            raise SpiceAuthError(401, "missing or invalid Authorization header")
        return authorization

    async def authenticate(
        self,
        *,
        authorization: str,
        client: str | None = None,
        auth_cookie: str | None = None,
    ) -> SpiceContexts:
        """Validate the bearer token with auth-service and return user contexts."""
        self.validate_authorization_header(authorization)
        headers: dict[str, str] = {
            "Authorization": authorization,
            "client": (client or "").strip() or self._default_client,
        }
        if auth_cookie:
            headers["auth-cookie"] = auth_cookie

        try:
            resp = await self._client.post(self._authenticate_url, headers=headers)
        except httpx.TimeoutException as exc:
            logger.error("spice auth-service timeout: %s", exc)
            raise SpiceAuthError(503, "authentication service unavailable") from exc
        except httpx.RequestError as exc:
            logger.error("spice auth-service unreachable: %s", exc)
            raise SpiceAuthError(503, "authentication service unavailable") from exc

        if resp.status_code >= 500:
            logger.error(
                "spice auth-service returned %s: %s",
                resp.status_code,
                resp.text[:200],
            )
            raise SpiceAuthError(503, "authentication service unavailable")
        if resp.status_code >= 400:
            raise SpiceAuthError(401, "invalid or expired token")

        return SpiceContexts.model_validate(resp.json())
