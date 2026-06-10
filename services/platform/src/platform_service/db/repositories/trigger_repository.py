"""W-8 — trigger_definition + module_trigger_binding repository."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.trigger_definition import (
    ModuleTriggerBinding,
    TriggerDefinition,
)


class TriggerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── TriggerDefinition CRUD ──────────────────────────────────────────

    async def create_trigger(
        self,
        *,
        trigger_kind: str,
        trigger_code: str,
        predicate_jsonb: dict[str, Any],
        description: str | None = None,
        predicate_schema_version: int = 1,
        tenant_id: UUID | None = None,
    ) -> TriggerDefinition:
        trigger = TriggerDefinition(
            trigger_kind=trigger_kind,
            trigger_code=trigger_code,
            description=description,
            predicate_jsonb=predicate_jsonb,
            predicate_schema_version=predicate_schema_version,
            tenant_id=tenant_id,
        )
        self._session.add(trigger)
        await self._session.flush()
        return trigger

    async def get_trigger(self, trigger_id: UUID) -> TriggerDefinition | None:
        return await self._session.get(TriggerDefinition, trigger_id)

    async def get_trigger_by_code(self, trigger_code: str) -> TriggerDefinition | None:
        result = await self._session.execute(
            select(TriggerDefinition).where(TriggerDefinition.trigger_code == trigger_code)
        )
        return result.scalar_one_or_none()

    async def list_active_triggers(
        self,
        *,
        trigger_kind: str | None = None,
        tenant_id: UUID | None = None,
    ) -> list[TriggerDefinition]:
        stmt = select(TriggerDefinition).where(TriggerDefinition.status == "active")
        if trigger_kind is not None:
            stmt = stmt.where(TriggerDefinition.trigger_kind == trigger_kind)
        if tenant_id is not None:
            stmt = stmt.where(TriggerDefinition.tenant_id == tenant_id)
        stmt = stmt.order_by(TriggerDefinition.trigger_code)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def deprecate_trigger(self, trigger_id: UUID) -> TriggerDefinition:
        """Soft-deprecate. Existing bindings remain visible to the evaluator
        for audit, but `status='deprecated'` causes the evaluator to skip
        firing — see TriggerEvaluator."""
        trigger = await self.get_trigger(trigger_id)
        if trigger is None:
            raise ValueError(f"trigger_definition {trigger_id} not found")
        trigger.status = "deprecated"
        trigger.updated_at = datetime.now(UTC)
        await self._session.flush()
        return trigger

    # ── ModuleTriggerBinding CRUD ───────────────────────────────────────

    async def bind_module_to_trigger(
        self,
        *,
        module_family_id: UUID,
        trigger_definition_id: UUID,
        relationship: str = "primary",
        priority_weight: int = 10,
        notes: str | None = None,
    ) -> ModuleTriggerBinding:
        if relationship not in ("primary", "secondary"):
            raise ValueError(f"relationship must be 'primary' or 'secondary', got {relationship!r}")
        binding = ModuleTriggerBinding(
            module_family_id=module_family_id,
            trigger_definition_id=trigger_definition_id,
            relationship=relationship,
            priority_weight=priority_weight,
            notes=notes,
        )
        self._session.add(binding)
        await self._session.flush()
        return binding

    async def list_bindings_for_trigger(self, trigger_id: UUID) -> list[ModuleTriggerBinding]:
        result = await self._session.execute(
            select(ModuleTriggerBinding)
            .where(ModuleTriggerBinding.trigger_definition_id == trigger_id)
            .order_by(ModuleTriggerBinding.priority_weight.desc())
        )
        return list(result.scalars().all())

    async def list_bindings_for_module_family(self, module_family_id: UUID) -> list[ModuleTriggerBinding]:
        result = await self._session.execute(
            select(ModuleTriggerBinding).where(ModuleTriggerBinding.module_family_id == module_family_id)
        )
        return list(result.scalars().all())

    async def list_active_bindings_for_trigger_codes(
        self, trigger_codes: list[str]
    ) -> list[tuple[ModuleTriggerBinding, TriggerDefinition]]:
        """Join helper: return (binding, trigger) pairs for a list of trigger
        codes, filtered to active triggers only. Used by ModuleSelector."""
        if not trigger_codes:
            return []
        stmt = (
            select(ModuleTriggerBinding, TriggerDefinition)
            .join(
                TriggerDefinition,
                ModuleTriggerBinding.trigger_definition_id == TriggerDefinition.id,
            )
            .where(
                TriggerDefinition.trigger_code.in_(trigger_codes),
                TriggerDefinition.status == "active",
            )
            .order_by(ModuleTriggerBinding.priority_weight.desc())
        )
        result = await self._session.execute(stmt)
        return [(b, t) for b, t in result.all()]

    async def list_active_triggers_updated_since(
        self,
        since: datetime,
        *,
        tenant_id: UUID | None = None,
    ) -> list[TriggerDefinition]:
        stmt = (
            select(TriggerDefinition)
            .where(
                TriggerDefinition.status == "active",
                TriggerDefinition.updated_at > since,
            )
            .order_by(TriggerDefinition.updated_at.asc(), TriggerDefinition.id.asc())
        )
        if tenant_id is not None:
            stmt = stmt.where(
                or_(
                    TriggerDefinition.tenant_id.is_(None),
                    TriggerDefinition.tenant_id == tenant_id,
                )
            )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_bindings_for_trigger_ids(self, trigger_ids: list[UUID]) -> list[ModuleTriggerBinding]:
        if not trigger_ids:
            return []
        stmt = (
            select(ModuleTriggerBinding)
            .where(ModuleTriggerBinding.trigger_definition_id.in_(trigger_ids))
            .order_by(
                ModuleTriggerBinding.trigger_definition_id.asc(),
                ModuleTriggerBinding.priority_weight.desc(),
                ModuleTriggerBinding.id.asc(),
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())
