"""Full-app integration tests with SPICE auth + authorization middleware."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.api.morning import router as morning_router
from platform_service.auth.rate_limit_middleware import RateLimitMiddleware
from platform_service.auth.spice_auth_middleware import SpiceAuthMiddleware
from platform_service.auth.spice_authorization_middleware import SpiceAuthorizationMiddleware
from platform_service.auth.spice_context import SpiceContexts, SpiceUserContext
from platform_service.config import Settings, get_settings
from platform_service.integrations.spice_auth_client import SpiceAuthClient
from pydantic_settings import SettingsConfigDict

from tests.conftest import platform_path

API_ROOT = "/medtronics-api"
VALID_TOKEN = "Bearer test.jwt.token"
TENANT_UUID = uuid4()

DEVICE_USER = SpiceUserContext.model_validate(
    {
        "id": 42,
        "username": "chw_user",
        "tenantId": 7,
        "roles": [{"name": "CHW", "suiteAccessName": "mob"}],
    }
)
def _contexts_for(user: SpiceUserContext) -> SpiceContexts:
    return SpiceContexts(user_detail=user, tenants=None)


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
    client.authenticate = AsyncMock(return_value=_contexts_for(DEVICE_USER))  # type: ignore[method-assign]
    return client


@pytest_asyncio.fixture
async def integration_client(
    mock_spice_client: SpiceAuthClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("SPICE_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_ROOT_PATH", API_ROOT)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("SPICE_TENANT_ID_MAP", f'{{"7": "{TENANT_UUID}"}}')
    get_settings.cache_clear()

    app = FastAPI()
    api_router = APIRouter(prefix=API_ROOT)
    api_router.include_router(morning_router)
    app.include_router(api_router)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(SpiceAuthorizationMiddleware)
    app.add_middleware(SpiceAuthMiddleware, client=mock_spice_client)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_device_user_cannot_query_other_chw_id(
    integration_client: AsyncClient,
) -> None:
    resp = await integration_client.get(
        platform_path("/morning/cards"),
        params={"chw_id": 99},
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_device_user_can_query_own_chw_id(
    integration_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mc_contracts.morning import MorningCardsResponse

    monkeypatch.setattr(
        "platform_service.api.morning.MorningSuggestionService.get_morning_cards",
        AsyncMock(return_value=MorningCardsResponse(items=[], total_points=0)),
    )
    resp = await integration_client.get(
        platform_path("/morning/cards"),
        params={"chw_id": 42},
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected(
    integration_client: AsyncClient,
) -> None:
    resp = await integration_client.get(platform_path("/morning/cards"))
    assert resp.status_code == 401
