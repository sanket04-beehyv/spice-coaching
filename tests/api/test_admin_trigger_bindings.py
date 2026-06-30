"""Admin trigger-binding API tests."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from platform_service.db.models.trigger_definition import (
    ModuleTriggerBinding,
    TriggerDefinition,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.api.conftest import (
    _seed_module,
)
from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


class TestTriggerBindings:
    async def _seed_trigger_and_binding(
        self,
        db_session: AsyncSession,
        module_id: UUID,
        *,
        relationship: str = "primary",
        priority_weight: int = 10,
    ) -> tuple[TriggerDefinition, ModuleTriggerBinding]:
        td = TriggerDefinition(
            trigger_kind="gap",
            trigger_code=f"gap:test-{uuid4().hex[:6]}",
            predicate_jsonb={"behavioural_gap_code": "test"},
        )
        db_session.add(td)
        await db_session.flush()
        binding = ModuleTriggerBinding(
            trigger_definition_id=td.id,
            module_id=module_id,
            relationship=relationship,
            priority_weight=priority_weight,
        )
        db_session.add(binding)
        await db_session.flush()
        await db_session.commit()
        return td, binding

    async def test_list_by_module(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(db_session)
        td, binding = await self._seed_trigger_and_binding(
            db_session, m.id, relationship="primary", priority_weight=10
        )

        resp = await client.get(platform_path(f"/admin/trigger-bindings/by-module/{m.id}"))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["module_id"] == str(m.id)
        assert data[0]["trigger_definition_id"] == str(td.id)
        assert data[0]["relationship"] == "primary"
        assert data[0]["priority_weight"] == 10

    async def test_create_binding(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(db_session)
        td = TriggerDefinition(
            trigger_kind="gap",
            trigger_code=f"gap:test-{uuid4().hex[:6]}",
            predicate_jsonb={"behavioural_gap_code": "test"},
        )
        db_session.add(td)
        await db_session.commit()

        resp = await client.post(
            platform_path("/admin/trigger-bindings"),
            json={
                "trigger_definition_id": str(td.id),
                "module_id": str(m.id),
                "relationship": "secondary",
                "priority_weight": 25,
                "notes": "covers refresher cadence",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["relationship"] == "secondary"
        assert data["priority_weight"] == 25
        assert data["notes"] == "covers refresher cadence"

    async def test_create_binding_uses_defaults(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(db_session)
        td = TriggerDefinition(
            trigger_kind="gap",
            trigger_code=f"gap:test-{uuid4().hex[:6]}",
            predicate_jsonb={"behavioural_gap_code": "test"},
        )
        db_session.add(td)
        await db_session.commit()

        resp = await client.post(
            platform_path("/admin/trigger-bindings"),
            json={
                "trigger_definition_id": str(td.id),
                "module_id": str(m.id),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["relationship"] == "primary"
        assert data["priority_weight"] == 10
        assert data["notes"] is None

    async def test_update_binding_priority_weight_only(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        m = await _seed_module(db_session)
        td, binding = await self._seed_trigger_and_binding(
            db_session, m.id, relationship="primary", priority_weight=10
        )

        # Update only priority_weight; relationship should remain "primary".
        resp = await client.put(
            platform_path(f"/admin/trigger-bindings/{binding.id}"),
            json={"priority_weight": 50},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["priority_weight"] == 50
        assert data["relationship"] == "primary"

    async def test_update_binding_relationship_validated(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        m = await _seed_module(db_session)
        td, binding = await self._seed_trigger_and_binding(db_session, m.id)
        resp = await client.put(
            platform_path(f"/admin/trigger-bindings/{binding.id}"),
            json={"relationship": "tertiary"},  # not in allowed set
        )
        assert resp.status_code == 400

    async def test_update_binding_404_for_unknown(self, client: AsyncClient) -> None:
        resp = await client.put(
            platform_path(f"/admin/trigger-bindings/{uuid4()}"),
            json={"priority_weight": 99},
        )
        assert resp.status_code == 404

    async def test_delete_binding(self, client: AsyncClient, db_session: AsyncSession) -> None:
        m = await _seed_module(db_session)
        td, binding = await self._seed_trigger_and_binding(db_session, m.id)

        resp = await client.delete(platform_path(f"/admin/trigger-bindings/{binding.id}"))
        assert resp.status_code == 200
        # And listing again returns no bindings for this module.
        list_resp = await client.get(platform_path(f"/admin/trigger-bindings/by-module/{m.id}"))
        assert list_resp.json() == []

    async def test_delete_binding_404_for_unknown(self, client: AsyncClient) -> None:
        resp = await client.delete(platform_path(f"/admin/trigger-bindings/{uuid4()}"))
        assert resp.status_code == 404
