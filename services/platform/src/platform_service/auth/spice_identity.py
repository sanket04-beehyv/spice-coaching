"""Bind request-scoped CHW identity to the authenticated SPICE principal."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request

from platform_service.auth.spice_context import SpiceUserContext
from platform_service.auth.spice_principal import is_admin_principal, is_device_principal
from platform_service.auth.spice_tenant_mapping import require_platform_tenant_for_spice_tenant
from platform_service.config import get_settings


def _spice_user(request: Request) -> SpiceUserContext | None:
    return getattr(request.state, "spice_user", None)


def _require_spice_user(request: Request) -> SpiceUserContext:
    user = _spice_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def resolve_chw_id_for_device_route(
    request: Request,
    requested_chw_id: int | None,
) -> int | None:
    """Return the CHW id for a device-plane route.

    When SPICE auth is enabled, device principals may only act as their own
    ``user.id``. Admin principals may pass an explicit ``requested_chw_id``.
    When auth is disabled, ``requested_chw_id`` is returned unchanged.
    """
    settings = get_settings()
    if not settings.spice_auth_enabled:
        return requested_chw_id

    user = _require_spice_user(request)
    if user.id is None:
        raise HTTPException(status_code=403, detail="authenticated user has no id")

    if is_admin_principal(user) and requested_chw_id is not None:
        return requested_chw_id

    if requested_chw_id is not None and requested_chw_id != user.id:
        raise HTTPException(
            status_code=403,
            detail=f"chw_id {requested_chw_id} does not match authenticated user",
        )
    return user.id


def require_chw_id_for_device_route(request: Request, requested_chw_id: int) -> int:
    """Like :func:`resolve_chw_id_for_device_route` but always returns an int."""
    resolved = resolve_chw_id_for_device_route(request, requested_chw_id)
    if resolved is None:
        raise HTTPException(status_code=400, detail="chw_id is required")
    return resolved


def require_chw_id_for_telemetry(request: Request, batch_chw_id: int) -> int:
    """Enforce that telemetry batch ``chw_id`` matches the authenticated device user."""
    settings = get_settings()
    if not settings.spice_auth_enabled:
        return batch_chw_id

    user = _require_spice_user(request)
    if user.id is None:
        raise HTTPException(status_code=403, detail="authenticated user has no id")

    if is_device_principal(user) and batch_chw_id != user.id:
        raise HTTPException(
            status_code=403,
            detail=f"batch chw_id {batch_chw_id} does not match authenticated user",
        )
    return batch_chw_id


def _tenant_from_spice_user(user: SpiceUserContext) -> UUID:
    return require_platform_tenant_for_spice_tenant(user.tenant_id)


def resolve_tenant_id_for_device_route(
    request: Request,
    requested_tenant_id: UUID | None,
) -> UUID | None:
    """Return tenant UUID for a device-plane route.

    When auth is disabled, ``requested_tenant_id`` is returned unchanged.
    When auth is enabled, device principals receive the mapped platform UUID
    for their SPICE ``tenantId``; tenant overrides are rejected. Admin
    principals may pass an explicit ``requested_tenant_id`` or fall back to
    their own mapped tenant.
    """
    settings = get_settings()
    if not settings.spice_auth_enabled:
        return requested_tenant_id

    user = _require_spice_user(request)
    if is_admin_principal(user):
        if requested_tenant_id is not None:
            return requested_tenant_id
        return _tenant_from_spice_user(user)

    if requested_tenant_id is not None and requested_tenant_id != UUID(int=0):
        raise HTTPException(
            status_code=403,
            detail="tenant_id override is not permitted for device principals",
        )
    return _tenant_from_spice_user(user)


def resolve_tenant_id_for_admin(
    request: Request,
    requested_tenant_id: UUID | None = None,
) -> UUID | None:
    """Resolve tenant scope for admin-plane routes (modules, ingest).

    When auth is disabled, returns ``requested_tenant_id`` unchanged (``None`` = global).
    When auth is enabled, device principals receive their mapped tenant; admin principals
    may pass an explicit ``requested_tenant_id`` or fall back to their mapped tenant.
    """
    return resolve_tenant_id_for_dashboard(request, requested_tenant_id)


def resolve_tenant_id_for_dashboard(
    request: Request,
    requested_tenant_id: UUID | None = None,
) -> UUID | None:
    """Resolve tenant scope for dashboard analytics when auth is enabled."""
    settings = get_settings()
    if not settings.spice_auth_enabled:
        return requested_tenant_id

    user = _require_spice_user(request)
    if requested_tenant_id is not None and is_admin_principal(user):
        return requested_tenant_id
    if is_device_principal(user):
        return _tenant_from_spice_user(user)
    if is_admin_principal(user):
        return _tenant_from_spice_user(user)
    return requested_tenant_id
