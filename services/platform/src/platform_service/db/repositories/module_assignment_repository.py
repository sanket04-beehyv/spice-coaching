"""Repository for CHWModuleAssignment database operations."""

from __future__ import annotations

from uuid import UUID

from mc_contracts.localized import LocalizedString
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_module_assignment import CHWModuleAssignment
from platform_service.db.models.module import Module


class ModuleAssignmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_assignments(
        self,
        module_id: UUID | None = None,
        assignment_type: str | None = None,
    ) -> list[tuple[CHWModuleAssignment, LocalizedString]]:
        """List active module assignments with module titles."""
        stmt = select(
            CHWModuleAssignment,
            Module.title_localized,
        ).join(Module, CHWModuleAssignment.module_id == Module.id)
        if module_id:
            stmt = stmt.where(CHWModuleAssignment.module_id == module_id)
        if assignment_type:
            stmt = stmt.where(CHWModuleAssignment.assignment_type == assignment_type)

        result = await self._session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def get_assignment_by_id(self, assignment_id: UUID) -> CHWModuleAssignment | None:
        """Fetch assignment by ID."""
        return await self._session.get(CHWModuleAssignment, assignment_id)

    async def delete_assignment(self, assignment: CHWModuleAssignment) -> None:
        """Delete/revoke assignment."""
        await self._session.delete(assignment)

    async def find_user_assignment(self, module_id: UUID, user_id: int) -> CHWModuleAssignment | None:
        """Find an existing user assignment."""
        stmt = select(CHWModuleAssignment).where(
            CHWModuleAssignment.module_id == module_id, CHWModuleAssignment.user_id == user_id
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def find_geo_assignment(self, module_id: UUID, upazila: str) -> CHWModuleAssignment | None:
        """Find an existing geographical assignment."""
        stmt = select(CHWModuleAssignment).where(
            CHWModuleAssignment.module_id == module_id, CHWModuleAssignment.upazila == upazila
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def find_group_assignment(self, module_id: UUID, tenant_id: int) -> CHWModuleAssignment | None:
        """Find an existing group/tenant assignment."""
        stmt = select(CHWModuleAssignment).where(
            CHWModuleAssignment.module_id == module_id, CHWModuleAssignment.tenant_id == tenant_id
        )
        return (await self._session.execute(stmt)).scalars().first()

    def add_assignment(self, assignment: CHWModuleAssignment) -> None:
        """Add a new assignment to the session."""
        self._session.add(assignment)
