"""CHW module training request submission with immediate module access."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.chw_module_assignment import CHWModuleAssignment
from platform_service.db.repositories.module_assignment_repository import ModuleAssignmentRepository
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.db.repositories.training_request_repository import TrainingRequestRepository
from platform_service.services.sync.module_assignment_resolver import resolve_assigned_module_ids


class InvalidModuleError(Exception):
    """Requested module is unknown, not published, or not available for CHW training."""


class DuplicateTrainingRequestError(Exception):
    """CHW already has access to this module."""


@dataclass(frozen=True)
class TrainingRequestSubmitResult:
    request_id: UUID
    module_id: UUID | None
    submitted_at: datetime


class TrainingRequestService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = TrainingRequestRepository(session)
        self._modules = ModuleRepository(session)
        self._assignments = ModuleAssignmentRepository(session)

    async def submit(
        self,
        *,
        chw_id: int,
        module_id: UUID | None,
        requested_module_name: str | None,
        reason: str | None,
        tenant_id: UUID | None,
    ) -> TrainingRequestSubmitResult:
        if module_id is not None:
            module = await self._modules.get_module(module_id)
            if module is None or module.lifecycle_status != "published":
                raise InvalidModuleError
            if module.chatbot_faqs_only:
                raise InvalidModuleError
            if tenant_id is not None and module.tenant_id is not None and module.tenant_id != tenant_id:
                raise InvalidModuleError

            assigned_module_ids = await resolve_assigned_module_ids(self._session, user_id=chw_id)
            if module.id in assigned_module_ids:
                raise DuplicateTrainingRequestError

            if await self._repo.has_for_module(chw_id=chw_id, module_id=module.id):
                raise DuplicateTrainingRequestError

            row = await self._repo.create(
                chw_id=chw_id,
                module_id=module.id,
                requested_module_name=None,
                reason=reason,
                tenant_id=tenant_id,
            )
            await self._ensure_individual_assignment(chw_id=chw_id, module_id=module.id)

            return TrainingRequestSubmitResult(
                request_id=row.id,
                module_id=module.id,
                submitted_at=row.submitted_at,
            )

        custom_name = (requested_module_name or "").strip()
        if not custom_name:
            raise InvalidModuleError
        if await self._repo.has_for_custom_name(chw_id=chw_id, requested_module_name=custom_name):
            raise DuplicateTrainingRequestError

        row = await self._repo.create(
            chw_id=chw_id,
            module_id=None,
            requested_module_name=custom_name,
            reason=reason,
            tenant_id=tenant_id,
        )

        return TrainingRequestSubmitResult(
            request_id=row.id,
            module_id=None,
            submitted_at=row.submitted_at,
        )

    async def _ensure_individual_assignment(self, *, chw_id: int, module_id: UUID) -> None:
        existing = await self._assignments.find_user_assignment(module_id, chw_id)
        if existing is not None:
            return
        self._assignments.add_assignment(
            CHWModuleAssignment(
                module_id=module_id,
                assignment_type="individual",
                user_id=chw_id,
                assigned_by=chw_id,
            )
        )
        await self._session.flush()
