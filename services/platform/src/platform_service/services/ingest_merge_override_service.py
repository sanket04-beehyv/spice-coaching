"""Admin ingest override-merge — promote secondary dual-path module."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.module_availability import (
    LIFECYCLE_DRAFT,
    LIFECYCLE_RETIRED,
    LIFECYCLE_REVIEW_PENDING,
)
from platform_service.db.repositories.module_repository import (
    ModuleNotFoundError,
    ModuleRepository,
)


@dataclass(frozen=True)
class IngestMergeOverrideResult:
    primary_module_id: UUID
    secondary_module_id: UUID
    source_module_id: UUID
    secondary_lifecycle_status: str


class IngestMergeOverrideService:
    """Promote secondary merge path; retire primary + matched source."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._modules = ModuleRepository(session)

    async def override(self, module_id: UUID) -> IngestMergeOverrideResult:
        primary = await self._session.get(Module, module_id)
        if primary is None:
            raise AppError(
                ErrorCode.MODULE_NOT_FOUND.value,
                "module not found",
                status=404,
            )

        if primary.merge_secondary_module_id is None:
            raise AppError(
                ErrorCode.MERGE_OVERRIDE_NOT_PRIMARY.value,
                "module is not a dual-path merge primary",
                status=400,
            )

        if primary.lifecycle_status != LIFECYCLE_REVIEW_PENDING:
            raise AppError(
                ErrorCode.MERGE_OVERRIDE_NOT_REVIEW_PENDING.value,
                "override is only allowed while the primary is review_pending",
                status=409,
            )

        secondary = await self._session.get(Module, primary.merge_secondary_module_id)
        if secondary is None:
            raise AppError(
                ErrorCode.MODULE_NOT_FOUND.value,
                "secondary merge module not found",
                status=404,
            )
        if secondary.lifecycle_status != LIFECYCLE_REVIEW_PENDING:
            raise AppError(
                ErrorCode.MERGE_OVERRIDE_SECONDARY_UNAVAILABLE.value,
                "secondary merge module is not review_pending",
                status=409,
            )

        source_id = primary.merge_source_module_id or secondary.merge_source_module_id
        if source_id is None:
            raise AppError(
                ErrorCode.MERGE_OVERRIDE_SOURCE_UNAVAILABLE.value,
                "merge source module link is missing",
                status=422,
            )
        source = await self._session.get(Module, source_id)
        if source is None or source.lifecycle_status == LIFECYCLE_RETIRED:
            raise AppError(
                ErrorCode.MERGE_OVERRIDE_SOURCE_UNAVAILABLE.value,
                "matched source module is missing or already retired",
                status=409,
            )

        try:
            await self._modules.retire_module(primary.id)
            await self._modules.retire_module(source.id)
        except ModuleNotFoundError as exc:
            raise AppError(
                ErrorCode.MODULE_NOT_FOUND.value,
                str(exc),
                status=404,
            ) from exc

        secondary.lifecycle_status = LIFECYCLE_DRAFT
        secondary.supersedes_module_id = source.id
        await self._session.flush()
        await self._session.commit()

        return IngestMergeOverrideResult(
            primary_module_id=primary.id,
            secondary_module_id=secondary.id,
            source_module_id=source.id,
            secondary_lifecycle_status=secondary.lifecycle_status,
        )
