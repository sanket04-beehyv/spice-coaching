"""Tests for SPICE auth middleware and SpiceAuthClient."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.auth.spice_auth_middleware import SpiceAuthMiddleware
from platform_service.auth.spice_context import SpiceContexts
from platform_service.config import Settings, get_settings
from platform_service.integrations.spice_auth_client import (
    SpiceAuthClient,
    SpiceAuthError,
)
from platform_service.main import create_app
from pydantic_settings import SettingsConfigDict

API_ROOT = "/medtronics-api"
VALID_TOKEN = "Bearer test.jwt.token"
MOCK_CONTEXTS = SpiceContexts.model_validate(
    {"userDetail": {"id": 42, "username": "chw_user", "tenantId": 1}, "tenants": None}
)


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    get_settings.cache_clear()
    monkeypatch.setattr(
        Settings,
        "model_config",
        SettingsConfigDict(env_file=None, env_file_encoding="utf-8", extra="ignore"),
    )
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_spice_client() -> SpiceAuthClient:
    client = SpiceAuthClient(base_url="http://auth.test")
    client.authenticate = AsyncMock(return_value=MOCK_CONTEXTS)  # type: ignore[method-assign]
    return client


@pytest_asyncio.fixture
async def middleware_app(
    mock_spice_client: SpiceAuthClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("SPICE_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_ROOT_PATH", "/medtronics-api")
    get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(SpiceAuthMiddleware, client=mock_spice_client)

    @app.get(f"{API_ROOT}/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{API_ROOT}/probe")
    async def probe() -> dict[str, int | None]:
        return {"ok": 1}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_disabled_auth_allows_request_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_ROOT_PATH", "/medtronics-api")
    get_settings.cache_clear()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"{API_ROOT}/health")
    assert resp.status_code == 200
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_enabled_missing_token_returns_401(middleware_app: AsyncClient) -> None:
    resp = await middleware_app.get(f"{API_ROOT}/probe")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing or invalid Authorization header"


@pytest.mark.asyncio
async def test_enabled_invalid_token_prefix_returns_401(middleware_app: AsyncClient) -> None:
    resp = await middleware_app.get(
        f"{API_ROOT}/probe",
        headers={"Authorization": "Token abc"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_enabled_valid_token_proceeds(
    middleware_app: AsyncClient,
    mock_spice_client: SpiceAuthClient,
) -> None:
    resp = await middleware_app.get(
        f"{API_ROOT}/probe",
        headers={"Authorization": VALID_TOKEN, "client": "mob"},
    )
    assert resp.status_code == 200
    mock_spice_client.authenticate.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_health_exempt_when_auth_enabled(middleware_app: AsyncClient) -> None:
    resp = await middleware_app.get(f"{API_ROOT}/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_spice_auth_failure_returns_401(
    middleware_app: AsyncClient, mock_spice_client: SpiceAuthClient
) -> None:
    mock_spice_client.authenticate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SpiceAuthError(401, "invalid or expired token")
    )
    resp = await middleware_app.get(
        f"{API_ROOT}/probe",
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_spice_auth_unavailable_returns_503(
    middleware_app: AsyncClient,
    mock_spice_client: SpiceAuthClient,
) -> None:
    mock_spice_client.authenticate = AsyncMock(  # type: ignore[method-assign]
        side_effect=SpiceAuthError(503, "authentication service unavailable")
    )
    resp = await middleware_app.get(
        f"{API_ROOT}/probe",
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 503


def test_spice_auth_exempt_path_set_default() -> None:
    s = Settings()
    assert f"{s.api_root_path_normalized}/health" in s.spice_auth_exempt_path_set
    assert f"{s.api_root_path_normalized}/ready" in s.spice_auth_exempt_path_set


def test_spice_auth_authenticate_url() -> None:
    s = Settings(spice_auth_base_url="http://gateway/auth-service")
    assert s.spice_auth_authenticate_url == "http://gateway/auth-service/authenticate"


@pytest.mark.asyncio
async def test_spice_auth_client_success(monkeypatch: pytest.MonkeyPatch) -> None:
    contexts_payload = {"userDetail": {"id": 7, "username": "u"}, "tenants": []}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/authenticate"
        assert request.headers["Authorization"] == VALID_TOKEN
        assert request.headers["client"] == "mob"
        return httpx.Response(200, json=contexts_payload)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "platform_service.integrations.spice_auth_client.httpx.AsyncClient",
        lambda timeout: _MockClientContext(transport, timeout),
    )
    client = SpiceAuthClient(base_url="http://auth.test", timeout=1.0)
    result = await client.authenticate(authorization=VALID_TOKEN, client="mob")
    assert result.user_detail is not None
    assert result.user_detail.id == 7


class _MockClientContext:
    def __init__(self, transport: httpx.MockTransport, timeout: float) -> None:
        self._transport = transport

    async def __aenter__(self) -> _MockClientContext:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        request = httpx.Request("POST", url, headers=kwargs.get("headers"))
        return self._transport.handle_request(request)


@pytest.mark.asyncio
async def test_spice_auth_client_4xx_maps_to_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "platform_service.integrations.spice_auth_client.httpx.AsyncClient",
        lambda timeout: _MockClientContext(transport, timeout),
    )
    client = SpiceAuthClient(base_url="http://auth.test", timeout=1.0)
    with pytest.raises(SpiceAuthError) as exc_info:
        await client.authenticate(authorization=VALID_TOKEN)
    assert exc_info.value.status_code == 401
