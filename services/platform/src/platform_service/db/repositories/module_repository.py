"""Module repository — admin dashboard reads + edits.

Read and write implementations are split across ``module_read_repository`` and
``module_write_repository``; this module exposes the combined façade used by
API handlers and services.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.module_read_repository import ModuleReadRepository
from platform_service.db.repositories.module_repository_helpers import (
    ModuleNotFoundError,
    ModuleVersionConflictError,
)
from platform_service.db.repositories.module_write_repository import ModuleWriteRepository


class ModuleRepository(ModuleReadRepository, ModuleWriteRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session


__all__ = ["ModuleRepository", "ModuleNotFoundError", "ModuleVersionConflictError"]
