"""Module admin lifecycle — deactivate, reactivate, lifecycle history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.models.module_lifecycle_event import ModuleLifecycleEvent
from platform_service.db.module_availability import LIFECYCLE_DEACTIVATED, LIFECYCLE_PUBLISHED


class ModuleNotFoundError(Exception):
    def __init__(self, module_id: UUID) -> None:
        super().__init__(f"module {module_id} not found")
        self.module_id = module_id


class ModuleLifecycleError(Exception):
    """Business-rule violation for activate/deactivate actions."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class ModuleLifecycleState:
    module_id: UUID
    module_family_id: UUID
    lifecycle_status: str
    first_activated_at: datetime | None
    last_deactivated_at: datetime | None
    last_reactivated_at: datetime | None


class ModuleLifecycleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_first_activation(
        self,
        module_id: UUID,
        *,
        at: datetime | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        module = await self._session.get(Module, module_id)
        if module is None:
            return
        ts = at or datetime.now(UTC)
        if module.first_activated_at is None:
            module.first_activated_at = ts
            await self._append_event(
                module_id=module_id,
                event_type="activated",
                occurred_at=ts,
                actor_id=actor_id,
            )
        await self._session.flush()

    async def deactivate(
        self,
        module_id: UUID,
        *,
        actor_id: UUID | None = None,
        reason: str | None = None,
    ) -> ModuleLifecycleState:
        module = await self._session.get(Module, module_id)
        if module is None:
            raise ModuleNotFoundError(module_id)
        if module.lifecycle_status == LIFECYCLE_DEACTIVATED:
            raise ModuleLifecycleError("module is already deactivated")
        if module.lifecycle_status != LIFECYCLE_PUBLISHED:
            raise ModuleLifecycleError(
                "only published modules can be deactivated; use retire for one-way removal"
            )

        ts = datetime.now(UTC)
        module.lifecycle_status = LIFECYCLE_DEACTIVATED
        module.last_deactivated_at = ts
        module.deactivated_by = actor_id
        await self._append_event(
            module_id=module_id,
            event_type="deactivated",
            occurred_at=ts,
            actor_id=actor_id,
            reason=reason,
        )
        await self._session.flush()
        return self._to_state(module)

    async def reactivate(
        self,
        module_id: UUID,
        *,
        actor_id: UUID | None = None,
        reason: str | None = None,
    ) -> ModuleLifecycleState:
        module = await self._session.get(Module, module_id)
        if module is None:
            raise ModuleNotFoundError(module_id)
        if module.lifecycle_status == LIFECYCLE_PUBLISHED:
            raise ModuleLifecycleError("module is already published")
        if module.lifecycle_status != LIFECYCLE_DEACTIVATED:
            raise ModuleLifecycleError("only deactivated modules can be reactivated")

        ts = datetime.now(UTC)
        module.lifecycle_status = LIFECYCLE_PUBLISHED
        module.last_reactivated_at = ts
        module.reactivated_by = actor_id
        if module.first_activated_at is None:
            module.first_activated_at = ts
        await self._append_event(
            module_id=module_id,
            event_type="reactivated",
            occurred_at=ts,
            actor_id=actor_id,
            reason=reason,
        )
        await self._session.flush()
        return self._to_state(module)

    async def list_lifecycle_events(self, module_id: UUID) -> list[ModuleLifecycleEvent]:
        result = await self._session.execute(
            select(ModuleLifecycleEvent)
            .where(ModuleLifecycleEvent.module_id == module_id)
            .order_by(ModuleLifecycleEvent.occurred_at.asc(), ModuleLifecycleEvent.id.asc())
        )
        return list(result.scalars().all())

    async def _append_event(
        self,
        *,
        module_id: UUID,
        event_type: str,
        occurred_at: datetime,
        actor_id: UUID | None = None,
        reason: str | None = None,
    ) -> ModuleLifecycleEvent:
        event = ModuleLifecycleEvent(
            id=uuid4(),
            module_id=module_id,
            event_type=event_type,
            occurred_at=occurred_at,
            actor_id=actor_id,
            reason=reason,
        )
        self._session.add(event)
        return event

    @staticmethod
    def _to_state(module: Module) -> ModuleLifecycleState:
        return ModuleLifecycleState(
            module_id=module.id,
            module_family_id=module.module_family_id,
            lifecycle_status=module.lifecycle_status,
            first_activated_at=module.first_activated_at,
            last_deactivated_at=module.last_deactivated_at,
            last_reactivated_at=module.last_reactivated_at,
        )
