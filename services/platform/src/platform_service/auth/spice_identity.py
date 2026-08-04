"""Bind request-scoped CHW identity to the authenticated SPICE principal."""

from __future__ import annotations

from uuid import UUID

from fastapi import Request
from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError

from platform_service.auth.spice_context import SpiceUserContext
from platform_service.auth.spice_principal import (
    is_admin_principal,
    is_device_principal,
    is_organizer_principal,
)
from platform_service.auth.spice_tenant_mapping import require_platform_tenant_for_spice_tenant
from platform_service.config import get_settings


def _spice_user(request: Request) -> SpiceUserContext | None:
    return getattr(request.state, "spice_user", None)


def _require_spice_user(request: Request) -> SpiceUserContext:
    user = _spice_user(request)
    if user is None:
        raise AppError(ErrorCode.NOT_AUTHENTICATED.value, "not authenticated", status=401)
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
        raise AppError(ErrorCode.FORBIDDEN.value, "authenticated user has no id", status=403)

    if is_admin_principal(user) and requested_chw_id is not None:
        return requested_chw_id

    if requested_chw_id is not None and requested_chw_id != user.id:
        raise AppError(
            ErrorCode.FORBIDDEN.value,
            f"chw_id {requested_chw_id} does not match authenticated user",
            status=403,
        )
    return user.id


def require_chw_id_for_device_route(request: Request, requested_chw_id: int) -> int:
    """Like :func:`resolve_chw_id_for_device_route` but always returns an int."""
    resolved = resolve_chw_id_for_device_route(request, requested_chw_id)
    if resolved is None:
        raise AppError(ErrorCode.CHW_ID_REQUIRED.value, "chw_id is required", status=400)
    return resolved


def require_chw_id_for_telemetry(request: Request, batch_chw_id: int) -> int:
    """Enforce that telemetry batch ``chw_id`` matches the authenticated device user."""
    settings = get_settings()
    if not settings.spice_auth_enabled:
        return batch_chw_id

    user = _require_spice_user(request)
    if user.id is None:
        raise AppError(ErrorCode.FORBIDDEN.value, "authenticated user has no id", status=403)

    if is_device_principal(user) and batch_chw_id != user.id:
        raise AppError(
            ErrorCode.FORBIDDEN.value,
            f"batch chw_id {batch_chw_id} does not match authenticated user",
            status=403,
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
        raise AppError(
            ErrorCode.FORBIDDEN.value,
            "tenant_id override is not permitted for device principals",
            status=403,
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


def require_organizer_for_device_route(
    request: Request,
    requested_po_user_id: int | None,
) -> int:
    """Return the PO user ID for the team-activity member-questions route.

    Device principals (PO) always get their own user ID — they cannot view
    another PO's team. Admin principals must supply an explicit
    ``requested_po_user_id``. When auth is disabled, ``requested_po_user_id``
    must be provided explicitly.
    """
    settings = get_settings()
    if not settings.spice_auth_enabled:
        if requested_po_user_id is None:
            raise AppError(ErrorCode.CHW_ID_REQUIRED.value, "po_user_id is required", status=400)
        return requested_po_user_id

    user = _require_spice_user(request)

    # Admin principals take precedence; they must supply an explicit po_user_id.
    if is_admin_principal(user):
        if requested_po_user_id is None:
            raise AppError(
                ErrorCode.CHW_ID_REQUIRED.value, "po_user_id is required for admin callers", status=400
            )
        return requested_po_user_id

    # Device principals are permitted only if they hold the PO role.
    if is_device_principal(user):
        if not is_organizer_principal(user):
            raise AppError(
                ErrorCode.FORBIDDEN.value, "only program organizers may access team activity", status=403
            )
        if user.id is None:
            raise AppError(ErrorCode.FORBIDDEN.value, "authenticated user has no id", status=403)
        return user.id

    raise AppError(ErrorCode.FORBIDDEN.value, "principal has no recognized role", status=403)


def resolve_organizer_for_team_activity(request: Request) -> int | None:
    """Return organizer scope for ``GET /dashboard/team-activity``.

    When auth is disabled, returns ``None`` (unrestricted — all SK users).
    When auth is enabled, only PO device principals are allowed and their
    ``user.id`` is returned. Admin and non-PO principals are rejected.
    """
    settings = get_settings()
    if not settings.spice_auth_enabled:
        return None

    user = _require_spice_user(request)

    if is_admin_principal(user):
        raise AppError(
            ErrorCode.FORBIDDEN.value,
            "only program organizers may access team activity",
            status=403,
        )

    if is_device_principal(user):
        if not is_organizer_principal(user):
            raise AppError(
                ErrorCode.FORBIDDEN.value, "only program organizers may access team activity", status=403
            )
        if user.id is None:
            raise AppError(ErrorCode.FORBIDDEN.value, "authenticated user has no id", status=403)
        return user.id

    raise AppError(ErrorCode.FORBIDDEN.value, "principal has no recognized role", status=403)


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
