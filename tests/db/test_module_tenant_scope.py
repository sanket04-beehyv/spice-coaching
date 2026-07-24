"""Tenant scoping for admin module list."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.repositories.module_repository import ModuleRepository

from tests.db.conftest import _make_module

pytestmark = pytest.mark.requires_db


@pytest.mark.asyncio
async def test_list_modules_filters_by_tenant(db_session) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()

    mod_a = await _make_module(db_session, title_localized={"bn": "tenant-a"})
    mod_a.tenant_id = tenant_a
    mod_b = await _make_module(db_session, title_localized={"bn": "tenant-b"})
    mod_b.tenant_id = tenant_b
    await db_session.flush()

    repo = ModuleRepository(db_session)
    scoped = await repo.list_modules(tenant_id=tenant_a)
    ids = {m.id for m in scoped}
    assert mod_a.id in ids
    assert mod_b.id not in ids
