"""Admin module retire — cascade dual-path secondary when retiring primary."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.repositories.module_repository import (
    ModuleNotFoundError,
    ModuleRepository,
)


class ModuleRetireService:
    """Retire a module; if it is a dual-path merge primary, retire its secondary too."""

    def __init__(self, session: AsyncSession) -> None:
        self._modules = ModuleRepository(session)

    async def retire(self, module_id: UUID) -> Module:
        module = await self._modules.retire_module(module_id)
        secondary_id = module.merge_secondary_module_id
        if secondary_id is not None:
            try:
                await self._modules.retire_module(secondary_id)
            except ModuleNotFoundError:
                pass
        return module
