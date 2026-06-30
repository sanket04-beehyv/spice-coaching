"""Build trigger sync bundles for device sync."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from mc_contracts.sync import (
    ModuleTriggerBindingSyncPayload,
    TriggerDefinitionSyncPayload,
    TriggersSyncBundle,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.trigger_repository import TriggerRepository


class TriggersBundleBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def build(self, *, since: datetime, tenant_id: UUID | None = None) -> TriggersSyncBundle:
        trigger_repo = TriggerRepository(self._session)
        triggers = await trigger_repo.list_active_triggers_updated_since(since, tenant_id=tenant_id)
        trigger_ids = [trigger.id for trigger in triggers]
        bindings = await trigger_repo.list_bindings_for_trigger_ids(trigger_ids)

        return TriggersSyncBundle(
            triggers=[
                TriggerDefinitionSyncPayload(
                    id=trigger.id,
                    trigger_kind=trigger.trigger_kind,
                    trigger_code=trigger.trigger_code,
                    description=trigger.description,
                    predicate_jsonb=dict(trigger.predicate_jsonb or {}),
                    predicate_schema_version=trigger.predicate_schema_version,
                    status=trigger.status,
                    tenant_id=trigger.tenant_id,
                    created_at=trigger.created_at,
                    updated_at=trigger.updated_at,
                )
                for trigger in triggers
            ],
            bindings=[
                ModuleTriggerBindingSyncPayload(
                    id=binding.id,
                    trigger_definition_id=binding.trigger_definition_id,
                    module_id=binding.module_id,
                    relationship=binding.relationship,
                    priority_weight=binding.priority_weight,
                    notes=binding.notes,
                )
                for binding in bindings
            ],
            server_time_utc=datetime.now(UTC).isoformat(),
        )
