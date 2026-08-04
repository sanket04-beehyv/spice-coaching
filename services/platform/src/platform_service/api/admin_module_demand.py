"""Admin module demand summary APIs (assign lives on admin_assignments)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from mc_contracts.admin_module_demand import (
    ModuleDemandRequestorsResponse,
    ModuleDemandSummaryResponse,
)
from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import resolve_tenant_id_for_admin
from platform_service.deps import get_db
from platform_service.services.module_demand_service import ModuleDemandService

router = APIRouter(prefix="/admin", tags=["admin-module-demand"])


@router.get("/module-demand/summary", response_model=ModuleDemandSummaryResponse)
async def get_module_demand_summary(
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
) -> ModuleDemandSummaryResponse:
    """LLM narrative plus top-K requested modules categorized by availability.

    Served from a daily-precomputed snapshot (``refresh_module_demand_summary``
    Celery beat); on a cache miss it falls back to a live build. Top-K comes from
    config key ``module_demand_top_k`` (Configuration page). ClickHouse chatbot
    demand is soft-failed (form data still returned); this differs from pure
    dashboard analytics routes that return 502 when CH is down.
    """
    tenant_id = resolve_tenant_id_for_admin(request, tenant_id)
    service = ModuleDemandService(session)
    return await service.get_summary(tenant_id=tenant_id)


@router.get(
    "/module-demand/modules/{module_id}/requestors",
    response_model=ModuleDemandRequestorsResponse,
)
async def list_module_demand_requestors(
    request: Request,
    module_id: UUID,
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
) -> ModuleDemandRequestorsResponse:
    """List form + chatbot requestors for a module, with already_assigned flags."""
    tenant_id = resolve_tenant_id_for_admin(request, tenant_id)
    service = ModuleDemandService(session)
    try:
        return await service.get_requestors(module_id, tenant_id=tenant_id)
    except LookupError as exc:
        raise AppError(ErrorCode.MODULE_NOT_FOUND.value, str(exc), status=404) from exc
