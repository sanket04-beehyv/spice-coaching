"""Tests for SPICE identity binding on device-plane routes."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi import HTTPException
from platform_service.auth.spice_context import SpiceUserContext
from platform_service.auth.spice_identity import (
    require_chw_id_for_device_route,
    require_chw_id_for_telemetry,
    resolve_chw_id_for_device_route,
    resolve_tenant_id_for_device_route,
)
from platform_service.config import Settings, get_settings
from pydantic_settings import SettingsConfigDict
from starlette.requests import Request


def _request_with_user(user: SpiceUserContext | None) -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    request = Request(scope)
    if user is not None:
        request.state.spice_user = user
    return request


TENANT_UUID = uuid4()

DEVICE_USER = SpiceUserContext.model_validate(
    {
        "id": 42,
        "username": "chw_user",
        "tenantId": 7,
        "roles": [{"name": "CHW", "suiteAccessName": "mob"}],
    }
)
ADMIN_USER = SpiceUserContext.model_validate(
    {"id": 1, "username": "admin", "roles": [{"name": "Area Manager"}]}
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


class TestSpiceIdentityDisabled:
    def test_passes_through_when_auth_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPICE_AUTH_ENABLED", "false")
        get_settings.cache_clear()
        request = _request_with_user(None)
        assert resolve_chw_id_for_device_route(request, 99) == 99
        assert require_chw_id_for_telemetry(request, 99) == 99


class TestSpiceIdentityEnabled:
    @pytest.fixture(autouse=True)
    def _enable_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPICE_AUTH_ENABLED", "true")
        monkeypatch.setenv(
            "SPICE_TENANT_ID_MAP",
            f'{{"7": "{TENANT_UUID}"}}',
        )
        get_settings.cache_clear()

    def test_device_user_cannot_override_chw_id(self) -> None:
        request = _request_with_user(DEVICE_USER)
        with pytest.raises(HTTPException) as exc:
            resolve_chw_id_for_device_route(request, 99)
        assert exc.value.status_code == 403

    def test_device_user_gets_own_chw_id_when_omitted(self) -> None:
        request = _request_with_user(DEVICE_USER)
        assert resolve_chw_id_for_device_route(request, None) == 42

    def test_device_user_matching_chw_id(self) -> None:
        request = _request_with_user(DEVICE_USER)
        assert require_chw_id_for_device_route(request, 42) == 42

    def test_telemetry_rejects_mismatched_batch_chw_id(self) -> None:
        request = _request_with_user(DEVICE_USER)
        with pytest.raises(HTTPException) as exc:
            require_chw_id_for_telemetry(request, 99)
        assert exc.value.status_code == 403

    def test_telemetry_accepts_matching_batch_chw_id(self) -> None:
        request = _request_with_user(DEVICE_USER)
        assert require_chw_id_for_telemetry(request, 42) == 42

    def test_admin_may_query_other_chw_id(self) -> None:
        request = _request_with_user(ADMIN_USER)
        assert resolve_chw_id_for_device_route(request, 99) == 99

    def test_device_user_cannot_override_tenant_id(self) -> None:
        request = _request_with_user(DEVICE_USER)
        with pytest.raises(HTTPException) as exc:
            resolve_tenant_id_for_device_route(request, uuid4())
        assert exc.value.status_code == 403

    def test_device_user_receives_mapped_tenant_id(self) -> None:
        request = _request_with_user(DEVICE_USER)
        assert resolve_tenant_id_for_device_route(request, None) == TENANT_UUID

    def test_device_user_without_mapping_raises_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPICE_TENANT_ID_MAP", '{"99": "00000000-0000-0000-0000-000000000099"}')
        get_settings.cache_clear()
        request = _request_with_user(DEVICE_USER)
        with pytest.raises(HTTPException) as exc:
            resolve_tenant_id_for_device_route(request, None)
        assert exc.value.status_code == 403

    def test_unauthenticated_raises_401(self) -> None:
        request = _request_with_user(None)
        with pytest.raises(HTTPException) as exc:
            resolve_chw_id_for_device_route(request, 1)
        assert exc.value.status_code == 401
