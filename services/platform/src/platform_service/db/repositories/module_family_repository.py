"""Module family lookups — stable id across versions."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.module_availability import LIFECYCLE_PUBLISHED


class ModuleFamilyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, family_id: UUID) -> ModuleFamily | None:
        return await self._session.get(ModuleFamily, family_id)

    async def get_many(self, family_ids: list[UUID]) -> dict[UUID, ModuleFamily]:
        if not family_ids:
            return {}
        result = await self._session.execute(select(ModuleFamily).where(ModuleFamily.id.in_(family_ids)))
        return {row.id: row for row in result.scalars().all()}

    async def is_assignable(self, family_id: UUID) -> bool:
        """True when the family's current published module is assignable for training."""
        family = await self.get(family_id)
        if family is None or family.current_published_module_id is None:
            return False
        module = await self._session.get(Module, family.current_published_module_id)
        return (
            module is not None
            and module.lifecycle_status == LIFECYCLE_PUBLISHED
            and not module.chatbot_faqs_only
        )

