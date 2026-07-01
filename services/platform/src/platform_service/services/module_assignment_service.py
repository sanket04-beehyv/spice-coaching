"""Service for CHW module assignments business logic and orchestration."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mc_contracts.admin_assignments import (
    AssignmentCreateRequest,
    AssignmentResponse,
    UserResponse,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_module_assignment import CHWModuleAssignment
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_assignment_repository import ModuleAssignmentRepository
from platform_service.services.user_service import get_all_users


class AssignmentValidationError(Exception):
    """Raised when assignment validation fails."""

    pass


class AssignmentNotFoundError(Exception):
    """Raised when assignment is not found."""

    pass


class ModuleNotFoundError(Exception):
    """Raised when the target module is not found."""

    pass


class ModuleAssignmentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ModuleAssignmentRepository(session)

    async def list_assignments(
        self,
        module_id: UUID | None = None,
        assignment_type: str | None = None,
    ) -> list[AssignmentResponse]:
        """List active assignments with resolved module titles and user details."""
        users_by_id = {u["id"]: u for u in get_all_users()}

        rows = await self._repo.list_assignments(
            module_id=module_id,
            assignment_type=assignment_type,
        )

        assignments = []
        for assignment_model, title_localized in rows:
            user_info = None
            if assignment_model.user_id is not None:
                raw_user = users_by_id.get(assignment_model.user_id)
                if raw_user:
                    user_info = UserResponse(**raw_user)

            assignments.append(
                AssignmentResponse(
                    id=assignment_model.id,
                    module_id=assignment_model.module_id,
                    module_title=title_localized,
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
        body: AssignmentCreateRequest,
        assigned_by: int,
    ) -> dict[str, Any]:
        """Create or update module assignments for users, upazilas, or tenants."""
        # Verify module exists
        module = await self._session.get(Module, body.module_id)
        if not module:
            raise ModuleNotFoundError(f"Module with ID {body.module_id} not found")

        created_ids = []

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

                existing = await self._repo.find_user_assignment(body.module_id, user_id)
                if not existing:
                    new_assignment = CHWModuleAssignment(
                        module_id=body.module_id,
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
                existing = await self._repo.find_geo_assignment(body.module_id, upazila)
                if not existing:
                    new_assignment = CHWModuleAssignment(
                        module_id=body.module_id,
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
                existing = await self._repo.find_group_assignment(body.module_id, tenant_id)
                if not existing:
                    new_assignment = CHWModuleAssignment(
                        module_id=body.module_id,
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

        await self._session.commit()
        return {
            "assigned_count": len(created_ids),
            "assignment_ids": [str(x) for x in created_ids],
        }

    async def revoke_assignment(self, assignment_id: UUID) -> dict[str, Any]:
        """Revoke / delete an assignment by ID."""
        assignment = await self._repo.get_assignment_by_id(assignment_id)
        if not assignment:
            raise AssignmentNotFoundError(f"Assignment with ID {assignment_id} not found")

        await self._repo.delete_assignment(assignment)
        await self._session.commit()
        return {"id": str(assignment_id), "status": "revoked"}
