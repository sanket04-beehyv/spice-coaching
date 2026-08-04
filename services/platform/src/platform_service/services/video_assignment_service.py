"""Service for CHW video (source_document) assignments."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mc_contracts.admin_assignments import UserResponse
from mc_contracts.admin_video_assignments import (
    VideoAssignmentCreateRequest,
    VideoAssignmentResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_video_assignment import CHWVideoAssignment
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.video_assignment_repository import VideoAssignmentRepository
from platform_service.services.user_service import get_all_users


class AssignmentValidationError(Exception):
    """Raised when assignment validation fails."""


class AssignmentNotFoundError(Exception):
    """Raised when assignment is not found."""


class VideoNotFoundError(Exception):
    """Raised when the target video source document is not found or not a video."""


class VideoAssignmentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = VideoAssignmentRepository(session)

    async def list_assignments(
        self,
        source_document_id: UUID | None = None,
        assignment_type: str | None = None,
    ) -> list[VideoAssignmentResponse]:
        users_by_id = {u["id"]: u for u in get_all_users()}

        rows = await self._repo.list_assignments(
            source_document_id=source_document_id,
            assignment_type=assignment_type,
        )

        assignments: list[VideoAssignmentResponse] = []
        for assignment_model, video_title in rows:
            user_info = None
            if assignment_model.user_id is not None:
                raw_user = users_by_id.get(assignment_model.user_id)
                if raw_user:
                    user_info = UserResponse(**raw_user)

            assignments.append(
                VideoAssignmentResponse(
                    id=assignment_model.id,
                    source_document_id=assignment_model.source_document_id,
                    video_title=video_title,
                    assignment_type=assignment_model.assignment_type,
                    tenant_id=assignment_model.tenant_id,
                    user_id=assignment_model.user_id,
                    user=user_info,
                    upazila=assignment_model.upazila,
                    assigned_by=assignment_model.assigned_by,
                    assigned_at=assignment_model.assigned_at,
                    created_at=assignment_model.created_at,
                    updated_at=assignment_model.updated_at,
                )
            )
        return assignments

    async def create_assignments(
        self,
        body: VideoAssignmentCreateRequest,
        assigned_by: int,
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        doc = await self._session.get(SourceDocument, body.source_document_id)
        if doc is None or doc.source_type != "video":
            raise VideoNotFoundError(f"Video source document with ID {body.source_document_id} not found")

        created_ids: list[UUID] = []

        if body.assignment_type in ("individual", "po_sk"):
            if not body.user_ids:
                raise AssignmentValidationError("user_ids must be provided for user assignment")

            users_by_id = {u["id"]: u for u in get_all_users()}

            for user_id in body.user_ids:
                if user_id not in users_by_id:
                    raise AssignmentValidationError(f"User with ID {user_id} not found")

                user_role = users_by_id[user_id]["role"]
                if body.assignment_type == "po_sk" and user_role != "PO":
                    raise AssignmentValidationError(
                        f"User with ID {user_id} is not a PO (required for po_sk)"
                    )

                existing = await self._repo.find_user_assignment(body.source_document_id, user_id)
                if not existing:
                    new_assignment = CHWVideoAssignment(
                        source_document_id=body.source_document_id,
                        assignment_type=body.assignment_type,
                        user_id=user_id,
                        assigned_by=assigned_by,
                    )
                    self._repo.add_assignment(new_assignment)
                    await self._session.flush()
                    created_ids.append(new_assignment.id)
                else:
                    if existing.assignment_type != body.assignment_type:
                        existing.assignment_type = body.assignment_type
                        await self._session.flush()
                    created_ids.append(existing.id)

        elif body.assignment_type == "geographical":
            if not body.upazilas:
                raise AssignmentValidationError("upazilas must be provided for geographical assignment")

            for upazila in body.upazilas:
                existing = await self._repo.find_geo_assignment(body.source_document_id, upazila)
                if not existing:
                    new_assignment = CHWVideoAssignment(
                        source_document_id=body.source_document_id,
                        assignment_type="geographical",
                        upazila=upazila,
                        assigned_by=assigned_by,
                    )
                    self._repo.add_assignment(new_assignment)
                    await self._session.flush()
                    created_ids.append(new_assignment.id)
                else:
                    created_ids.append(existing.id)

        elif body.assignment_type == "group":
            if not body.tenant_ids:
                raise AssignmentValidationError("tenant_ids must be provided for group assignment")

            for tenant_id in body.tenant_ids:
                existing = await self._repo.find_group_assignment(body.source_document_id, tenant_id)
                if not existing:
                    new_assignment = CHWVideoAssignment(
                        source_document_id=body.source_document_id,
                        assignment_type="group",
                        tenant_id=tenant_id,
                        assigned_by=assigned_by,
                    )
                    self._repo.add_assignment(new_assignment)
                    await self._session.flush()
                    created_ids.append(new_assignment.id)
                else:
                    created_ids.append(existing.id)
        else:
            raise AssignmentValidationError("Invalid assignment_type")

        if commit:
            await self._session.commit()
        else:
            await self._session.flush()
        return {
            "assigned_count": len(created_ids),
            "assignment_ids": [str(x) for x in created_ids],
        }

    async def revoke_assignment(self, assignment_id: UUID) -> dict[str, Any]:
        assignment = await self._repo.get_assignment_by_id(assignment_id)
        if not assignment:
            raise AssignmentNotFoundError(f"Assignment with ID {assignment_id} not found")

        await self._repo.delete_assignment(assignment)
        await self._session.commit()
        return {"id": str(assignment_id), "status": "revoked"}
