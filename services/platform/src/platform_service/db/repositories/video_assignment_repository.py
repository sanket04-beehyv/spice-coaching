"""Repository for CHWVideoAssignment database operations."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_video_assignment import CHWVideoAssignment
from platform_service.db.models.source_document import SourceDocument


class VideoAssignmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_assignments(
        self,
        source_document_id: UUID | None = None,
        assignment_type: str | None = None,
    ) -> list[tuple[CHWVideoAssignment, str]]:
        """List video assignments with source document titles."""
        stmt = select(
            CHWVideoAssignment,
            SourceDocument.title,
        ).join(
            SourceDocument,
            CHWVideoAssignment.source_document_id == SourceDocument.id,
        )
        if source_document_id:
            stmt = stmt.where(CHWVideoAssignment.source_document_id == source_document_id)
        if assignment_type:
            stmt = stmt.where(CHWVideoAssignment.assignment_type == assignment_type)

        result = await self._session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]

    async def get_assignment_by_id(self, assignment_id: UUID) -> CHWVideoAssignment | None:
        return await self._session.get(CHWVideoAssignment, assignment_id)

    async def delete_assignment(self, assignment: CHWVideoAssignment) -> None:
        await self._session.delete(assignment)

    async def find_user_assignment(self, source_document_id: UUID, user_id: int) -> CHWVideoAssignment | None:
        stmt = select(CHWVideoAssignment).where(
            CHWVideoAssignment.source_document_id == source_document_id,
            CHWVideoAssignment.user_id == user_id,
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def find_geo_assignment(self, source_document_id: UUID, upazila: str) -> CHWVideoAssignment | None:
        stmt = select(CHWVideoAssignment).where(
            CHWVideoAssignment.source_document_id == source_document_id,
            CHWVideoAssignment.upazila == upazila,
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def find_group_assignment(
        self, source_document_id: UUID, tenant_id: int
    ) -> CHWVideoAssignment | None:
        stmt = select(CHWVideoAssignment).where(
            CHWVideoAssignment.source_document_id == source_document_id,
            CHWVideoAssignment.tenant_id == tenant_id,
        )
        return (await self._session.execute(stmt)).scalars().first()

    def add_assignment(self, assignment: CHWVideoAssignment) -> None:
        self._session.add(assignment)
