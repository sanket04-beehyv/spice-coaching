from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from mc_contracts.admin_assignments import (
    AssignmentCreateRequest,
    AssignmentResponse,
    UserResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.deps import get_db
from platform_service.services.module_assignment_service import (
    AssignmentNotFoundError,
    AssignmentValidationError,
    ModuleAssignmentService,
    ModuleNotFoundError,
)
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
