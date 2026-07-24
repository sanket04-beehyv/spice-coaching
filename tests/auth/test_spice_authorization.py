"""Tests for SPICE two-plane authorization (admin vs device suite access)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.auth.spice_auth_middleware import SpiceAuthMiddleware
from platform_service.auth.spice_authorization_middleware import SpiceAuthorizationMiddleware
from platform_service.auth.spice_context import SpiceContexts, SpiceUserContext
from platform_service.auth.spice_principal import is_admin_principal, is_device_principal
from platform_service.config import Settings, get_settings
from platform_service.integrations.spice_auth_client import SpiceAuthClient
from pydantic_settings import SettingsConfigDict

API_ROOT = "/medtronics-api"
VALID_TOKEN = "Bearer test.jwt.token"

ADMIN_USER = SpiceUserContext.model_validate(
    {
        "id": 1,
        "username": "region_admin",
        "roles": [{"name": "REGION_ADMIN", "suiteAccessName": "admin"}],
    }
)
AREA_MANAGER = SpiceUserContext.model_validate(
    {
        "id": 10,
        "username": "am_user",
        "roles": [{"name": "Area Manager"}],
    }
)
DIVISION_MANAGER = SpiceUserContext.model_validate(
    {
        "id": 11,
        "username": "dm_user",
        "roles": [{"name": "Division Manager"}],
    }
)
HEAD_OFFICE = SpiceUserContext.model_validate(
    {
        "id": 12,
        "username": "ho_user",
        "roles": [{"name": "Head Office"}],
    }
)
SUPER_ADMIN = SpiceUserContext.model_validate(
    {
        "id": 13,
        "username": "sa_user",
        "roles": [{"name": "Super Admin"}],
    }
)
DEVICE_USER = SpiceUserContext.model_validate(
    {
        "id": 2,
        "username": "chw_user",
        "roles": [{"name": "CHW", "suiteAccessName": "mob"}],
    }
)
SUPER_USER = SpiceUserContext.model_validate(
    {
        "id": 3,
        "username": "super",
        "isSuperUser": True,
        "roles": [{"name": "SUPER_USER", "suiteAccessName": "admin"}],
    }
)
JOB_USER = SpiceUserContext.model_validate(
    {
        "id": 4,
        "username": "job",
        "isJobUser": True,
        "roles": [{"name": "JOB_USER", "suiteAccessName": "mob"}],
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


@pytest_asyncio.fixture
async def authz_app(mock_spice_client: SpiceAuthClient) -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.add_middleware(SpiceAuthorizationMiddleware)
    app.add_middleware(SpiceAuthMiddleware, client=mock_spice_client)

    @app.get(f"{API_ROOT}/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{API_ROOT}/coaching/probe")
    async def device_probe() -> dict[str, str]:
        return {"plane": "device"}

    @app.get(f"{API_ROOT}/admin/probe")
    async def admin_probe() -> dict[str, str]:
        return {"plane": "admin"}

    @app.get(f"{API_ROOT}/dashboard/probe")
    async def dashboard_probe() -> dict[str, str]:
        return {"plane": "dashboard"}

    @app.get(f"{API_ROOT}/admin/ingest-probe")
    async def admin_ingest_probe() -> dict[str, str]:
        return {"plane": "admin_ingest"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_spice_client() -> SpiceAuthClient:
    client = SpiceAuthClient(base_url="http://auth.test")
    client.authenticate = AsyncMock(return_value=_contexts_for(DEVICE_USER))  # type: ignore[method-assign]
    return client


@pytest_asyncio.fixture
async def authz_client(
    authz_app: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setenv("SPICE_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_ROOT_PATH", "/medtronics-api")
    get_settings.cache_clear()
    yield authz_app
    get_settings.cache_clear()


def test_is_admin_principal() -> None:
    assert is_admin_principal(ADMIN_USER) is True
    assert is_admin_principal(AREA_MANAGER) is True
    assert is_admin_principal(DIVISION_MANAGER) is True
    assert is_admin_principal(HEAD_OFFICE) is True
    assert is_admin_principal(SUPER_ADMIN) is True
    assert is_admin_principal(DEVICE_USER) is False
    assert is_admin_principal(SUPER_USER) is True


def test_is_device_principal() -> None:
    assert is_device_principal(DEVICE_USER) is True
    assert is_device_principal(ADMIN_USER) is False
    assert is_device_principal(SUPER_USER) is True
    assert is_device_principal(JOB_USER) is True


@pytest.mark.asyncio
async def test_admin_principal_reaches_admin_path(
    authz_client: AsyncClient,
    mock_spice_client: SpiceAuthClient,
) -> None:
    mock_spice_client.authenticate = AsyncMock(return_value=_contexts_for(ADMIN_USER))  # type: ignore[method-assign]
    resp = await authz_client.get(
        f"{API_ROOT}/admin/probe",
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == {"plane": "admin"}


@pytest.mark.asyncio
async def test_device_principal_denied_admin_path(
    authz_client: AsyncClient,
) -> None:
    resp = await authz_client.get(
        f"{API_ROOT}/admin/probe",
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "insufficient role for this API"


@pytest.mark.asyncio
async def test_device_principal_reaches_device_path(
    authz_client: AsyncClient,
) -> None:
    resp = await authz_client.get(
        f"{API_ROOT}/coaching/probe",
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 200
    assert resp.json() == {"plane": "device"}


@pytest.mark.asyncio
async def test_admin_principal_denied_device_path(
    authz_client: AsyncClient,
    mock_spice_client: SpiceAuthClient,
) -> None:
    mock_spice_client.authenticate = AsyncMock(return_value=_contexts_for(ADMIN_USER))  # type: ignore[method-assign]
    resp = await authz_client.get(
        f"{API_ROOT}/coaching/probe",
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "insufficient role for this API"


@pytest.mark.asyncio
async def test_super_user_reaches_both_planes(
    authz_client: AsyncClient,
    mock_spice_client: SpiceAuthClient,
) -> None:
    mock_spice_client.authenticate = AsyncMock(return_value=_contexts_for(SUPER_USER))  # type: ignore[method-assign]
    headers = {"Authorization": VALID_TOKEN}
    assert (await authz_client.get(f"{API_ROOT}/admin/probe", headers=headers)).status_code == 200
    assert (await authz_client.get(f"{API_ROOT}/coaching/probe", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_dashboard_requires_admin_plane(
    authz_client: AsyncClient,
    mock_spice_client: SpiceAuthClient,
) -> None:
    mock_spice_client.authenticate = AsyncMock(return_value=_contexts_for(ADMIN_USER))  # type: ignore[method-assign]
    resp = await authz_client.get(
        f"{API_ROOT}/dashboard/probe",
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 200

    mock_spice_client.authenticate = AsyncMock(return_value=_contexts_for(DEVICE_USER))  # type: ignore[method-assign]
    resp = await authz_client.get(
        f"{API_ROOT}/dashboard/probe",
        headers={"Authorization": VALID_TOKEN},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_job_user_reaches_device_not_admin(
    authz_client: AsyncClient,
    mock_spice_client: SpiceAuthClient,
) -> None:
    mock_spice_client.authenticate = AsyncMock(return_value=_contexts_for(JOB_USER))  # type: ignore[method-assign]
    headers = {"Authorization": VALID_TOKEN}
    assert (await authz_client.get(f"{API_ROOT}/coaching/probe", headers=headers)).status_code == 200
    assert (await authz_client.get(f"{API_ROOT}/admin/probe", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_admin_ingest_requires_admin_role(
    authz_client: AsyncClient,
    mock_spice_client: SpiceAuthClient,
) -> None:
    mock_spice_client.authenticate = AsyncMock(return_value=_contexts_for(ADMIN_USER))  # type: ignore[method-assign]
    headers = {"Authorization": VALID_TOKEN}
    resp = await authz_client.get(f"{API_ROOT}/admin/ingest-probe", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"plane": "admin_ingest"}

    mock_spice_client.authenticate = AsyncMock(return_value=_contexts_for(DEVICE_USER))  # type: ignore[method-assign]
    resp = await authz_client.get(f"{API_ROOT}/admin/ingest-probe", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "insufficient role for this API"


@pytest.mark.asyncio
async def test_auth_disabled_skips_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPICE_AUTH_ENABLED", "false")
    get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(SpiceAuthorizationMiddleware)
    app.add_middleware(SpiceAuthMiddleware)

    @app.get(f"{API_ROOT}/admin/probe")
    async def admin_probe() -> dict[str, str]:
        return {"ok": "1"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"{API_ROOT}/admin/probe")
    assert resp.status_code == 200


def test_path_prefix_settings_defaults() -> None:
    s = Settings()
    assert "admin" in s.spice_admin_path_prefix_set
    assert "dashboard" in s.spice_admin_path_prefix_set
    assert "coaching" in s.spice_device_path_prefix_set
    assert "telemetry" in s.spice_device_path_prefix_set
