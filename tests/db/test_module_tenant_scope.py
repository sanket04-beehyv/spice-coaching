"""Tenant scoping for admin module list and ingest retire."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.services.ingest_upload_service import IngestUploadService

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


@pytest.mark.asyncio
async def test_retire_published_modules_if_new_scopes_to_tenant(db_session) -> None:
    tenant_a = uuid4()
    tenant_b = uuid4()

    mod_a = await _make_module(db_session, title_localized={"bn": "a"}, lifecycle_status="published")
    mod_a.tenant_id = tenant_a
    mod_b = await _make_module(db_session, title_localized={"bn": "b"}, lifecycle_status="published")
    mod_b.tenant_id = tenant_b
    await db_session.flush()

    upload_svc = IngestUploadService(db_session, MagicMock())
    _, retired_ids = await upload_svc.retire_published_modules_if_new("new", tenant_id=tenant_a)
    await db_session.commit()

    assert mod_a.id in retired_ids
    assert mod_b.id not in retired_ids

    refreshed_b = await db_session.get(Module, mod_b.id)
    assert refreshed_b is not None
    assert refreshed_b.lifecycle_status == "published"
