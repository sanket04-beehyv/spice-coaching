"""Map SPICE integer ``tenantId`` values to platform UUID tenant ids."""

from __future__ import annotations

from uuid import UUID

from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError

from platform_service.config import Settings, get_settings
from platform_service.tenant_mapping import parse_spice_tenant_id_map

__all__ = [
    "map_spice_tenant_to_platform",
    "parse_spice_tenant_id_map",
    "require_platform_tenant_for_spice_tenant",
]


def map_spice_tenant_to_platform(
    spice_tenant_id: int | None,
    *,
    settings: Settings | None = None,
) -> UUID | None:
    """Return the platform tenant UUID for a SPICE tenant id, or ``None`` if unmapped."""
    if spice_tenant_id is None:
        return None
    cfg = settings or get_settings()
    return cfg.spice_tenant_uuid_by_id.get(spice_tenant_id)


def require_platform_tenant_for_spice_tenant(
    spice_tenant_id: int | None,
    *,
    settings: Settings | None = None,
) -> UUID:
    """Resolve SPICE tenant id to platform UUID or raise HTTP 403."""
    if spice_tenant_id is None:
        raise AppError(
            ErrorCode.TENANT_MISMATCH.value,
            "authenticated user has no tenantId; tenant mapping required",
            status=403,
        )
    mapped = map_spice_tenant_to_platform(spice_tenant_id, settings=settings)
    if mapped is None:
        raise AppError(
            ErrorCode.TENANT_MISMATCH.value,
            f"no platform tenant mapping configured for SPICE tenantId={spice_tenant_id}",
            status=403,
        )
    return mapped
