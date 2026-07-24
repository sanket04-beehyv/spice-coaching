from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from mc_contracts.admin_assignments import (
    AssignmentCreateRequest,
    AssignmentResponse,
    UserResponse,
)
from mc_contracts.admin_module_demand import (
    ModuleDemandAssignRequest,
    ModuleDemandAssignResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import resolve_tenant_id_for_admin
from platform_service.deps import get_db
from platform_service.services.module_assignment_service import (
    AssignmentNotFoundError,
    AssignmentValidationError,
    ModuleAssignmentService,
    ModuleNotFoundError,
)
from platform_service.services.module_demand_service import ModuleDemandService
from platform_service.services.user_service import get_all_users

router = APIRouter(prefix="/admin", tags=["admin-dashboard"])


@router.get("/assignments", response_model=list[AssignmentResponse])
async def list_assignments(
    module_id: UUID | None = Query(None),
    assignment_type: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
) -> list[AssignmentResponse]:
    """List active module assignments with module title metadata."""
    service = ModuleAssignmentService(session)
    return await service.list_assignments(module_id=module_id, assignment_type=assignment_type)


@router.post("/assignments", status_code=201)
async def create_assignments(
    request: Request,
    body: AssignmentCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create new assignments for individuals, geographical groups, or organizations."""
    spice_user = getattr(request.state, "spice_user", None)
    assigned_by = spice_user.id if spice_user and spice_user.id is not None else 1

    service = ModuleAssignmentService(session)
    try:
        return await service.create_assignments(body, assigned_by)
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except AssignmentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/module-demand/modules/{module_id}/assign",
    response_model=ModuleDemandAssignResponse,
    status_code=201,
)
async def assign_module_from_demand(
    request: Request,
    module_id: UUID,
    body: ModuleDemandAssignRequest,
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    session: AsyncSession = Depends(get_db),
) -> ModuleDemandAssignResponse:
    """Bulk individual assign from the demand summary flow, with audit trail."""
    tenant_id = resolve_tenant_id_for_admin(request, tenant_id)
    spice_user = getattr(request.state, "spice_user", None)
    assigned_by = spice_user.id if spice_user and spice_user.id is not None else 1
    actor = str(assigned_by)

    service = ModuleDemandService(session)
    try:
        return await service.assign_to_requestors(
            module_id=module_id,
            user_ids=body.user_ids,
            assigned_by=assigned_by,
            actor=actor,
            tenant_id=tenant_id,
        )
    except ModuleNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssignmentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/assignments/{assignment_id}")
async def revoke_assignment(
    assignment_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Delete/revoke the specified assignment."""
    service = ModuleAssignmentService(session)
    try:
        return await service.revoke_assignment(assignment_id)
    except AssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/users", response_model=list[UserResponse])
async def list_users() -> list[UserResponse]:
    """List all hardcoded users with roles and geographical levels."""
    users = get_all_users()
    return [UserResponse(**u) for u in users]
