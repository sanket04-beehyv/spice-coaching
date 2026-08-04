"""Admin video (source_document) assignment endpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from mc_contracts.admin_video_assignments import (
    VideoAssignmentCreateRequest,
    VideoAssignmentResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.deps import get_db
from platform_service.services.video_assignment_service import (
    AssignmentNotFoundError,
    AssignmentValidationError,
    VideoAssignmentService,
    VideoNotFoundError,
)

router = APIRouter(prefix="/admin", tags=["admin-dashboard"])


@router.get("/video-assignments", response_model=list[VideoAssignmentResponse])
async def list_video_assignments(
    source_document_id: UUID | None = Query(None),
    assignment_type: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
) -> list[VideoAssignmentResponse]:
    """List active video assignments with source document title metadata."""
    service = VideoAssignmentService(session)
    return await service.list_assignments(
        source_document_id=source_document_id,
        assignment_type=assignment_type,
    )


@router.post("/video-assignments", status_code=201)
async def create_video_assignments(
    request: Request,
    body: VideoAssignmentCreateRequest,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create new video assignments for individuals, geographical groups, or organizations."""
    spice_user = getattr(request.state, "spice_user", None)
    assigned_by = spice_user.id if spice_user and spice_user.id is not None else 1

    service = VideoAssignmentService(session)
    try:
        return await service.create_assignments(body, assigned_by)
    except VideoNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssignmentValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/video-assignments/{assignment_id}")
async def revoke_video_assignment(
    assignment_id: UUID,
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Delete/revoke the specified video assignment."""
    service = VideoAssignmentService(session)
    try:
        return await service.revoke_assignment(assignment_id)
    except AssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
