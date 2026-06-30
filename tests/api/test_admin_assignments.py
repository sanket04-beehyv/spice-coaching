from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from platform_service.api.admin_assignments import router as admin_assignments_router
from platform_service.api.sync import router as sync_router
from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.deps import get_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def _wipe_data_between_tests(db_session: AsyncSession) -> AsyncIterator[None]:
    yield
    await db_session.rollback()
    await db_session.execute(
        text("TRUNCATE chw_module_assignment, module, module_family RESTART IDENTITY CASCADE")
    )
    await db_session.commit()


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> FastAPI:
    app_obj = FastAPI()

    # Custom mock auth middleware to inject spice_user for testing sync filtering
    @app_obj.middleware("http")
    async def mock_auth_middleware(request: Request, call_next):
        mock_user_id = request.headers.get("x-mock-user-id")
        mock_org_ids = request.headers.get("x-mock-org-ids")
        if mock_user_id:

            class MockSpiceUser:
                id = int(mock_user_id)
                organization_ids = [int(x) for x in mock_org_ids.split(",")] if mock_org_ids else []

            request.state.spice_user = MockSpiceUser()
        return await call_next(request)

    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(admin_assignments_router)
    api_router.include_router(sync_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app_obj.dependency_overrides[get_db] = _override_get_db
    yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_module(session: AsyncSession, title: str) -> Module:
    family = ModuleFamily(module_code=f"f-{uuid4().hex[:8]}")
    session.add(family)
    await session.flush()
    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": title, "en": title},
        domain="rmnch",
        module_type="refresher",
        lifecycle_status="published",
        module_json={"cards": [{"title": {"bn": "c"}}]},
        published_at=datetime.now(UTC),
    )
    session.add(module)
    await session.flush()
    family.current_published_module_id = module.id
    await session.flush()
    await session.commit()
    return module


class TestAdminAssignments:
    async def test_crud_assignment_workflow(self, client: AsyncClient, db_session: AsyncSession) -> None:
        module_1 = await _seed_module(db_session, "Module One")
        module_2 = await _seed_module(db_session, "Module Two")

        # 1. Create individual assignment
        resp = await client.post(
            platform_path("/admin/assignments"),
            json={
                "module_id": str(module_1.id),
                "assignment_type": "individual",
                "user_ids": [1313053891, 1313053895],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["assigned_count"] == 2
        assert len(data["assignment_ids"]) == 2

        # 2. Check no duplicates created when posting again
        resp = await client.post(
            platform_path("/admin/assignments"),
            json={
                "module_id": str(module_1.id),
                "assignment_type": "individual",
                "user_ids": [1313053891, 1313053892],
            },
        )
        assert resp.status_code == 201
        assert resp.json()["assigned_count"] == 2  # 1313053891 is existing, 1313053892 is new

        # 3. Create group/tenant assignment
        resp = await client.post(
            platform_path("/admin/assignments"),
            json={"module_id": str(module_2.id), "assignment_type": "group", "tenant_ids": [5001]},
        )
        assert resp.status_code == 201

        # 4. List assignments
        resp = await client.get(platform_path("/admin/assignments"))
        assert resp.status_code == 200
        assignments = resp.json()
        # Should contain assignments for 1313053891, 1313053895, 1313053892, 5001 (total 4)
        assert len(assignments) == 4

        # Verify module titles are populated
        a1 = [a for a in assignments if a["module_id"] == str(module_1.id)]
        assert len(a1) == 3
        assert a1[0]["module_title"]["bn"] == "Module One"

        # 5. Delete/Revoke assignment
        assignment_id_to_revoke = assignments[0]["id"]
        resp = await client.delete(platform_path(f"/admin/assignments/{assignment_id_to_revoke}"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"

        # List again and verify it is gone
        resp = await client.get(platform_path("/admin/assignments"))
        assert len(resp.json()) == 3

    async def test_sync_filtering_by_assignment(self, client: AsyncClient, db_session: AsyncSession) -> None:
        module_1 = await _seed_module(db_session, "Module One")
        module_2 = await _seed_module(db_session, "Module Two")
        await _seed_module(db_session, "Module Three")

        # Assign module 1 to user 1313053891
        await client.post(
            platform_path("/admin/assignments"),
            json={"module_id": str(module_1.id), "assignment_type": "individual", "user_ids": [1313053891]},
        )
        # Assign module 2 to group 789
        await client.post(
            platform_path("/admin/assignments"),
            json={"module_id": str(module_2.id), "assignment_type": "group", "tenant_ids": [789]},
        )
        # module 3 remains unassigned

        # 1. Sync without user_id returns all published modules
        resp = await client.get(platform_path("/sync/modules"), params={"since": "2020-01-01T00:00:00Z"})
        assert resp.status_code == 200
        assert len(resp.json()["modules"]) == 3
        assert resp.json()["assigned_module_ids"] == []

        # 2. Assigned modules for user 1313053891 (should return module 1 only)
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1313053891},
            headers={"x-mock-user-id": "1313053891"},
        )
        assert resp.status_code == 200
        assigned_module_ids = resp.json()["assigned_module_ids"]
        assert len(assigned_module_ids) == 1
        assert assigned_module_ids[0] == str(module_1.id)

        # 3. Assigned modules for user 1313053895 with organization 789 (should return module 2 only)
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1313053895},
            headers={"x-mock-user-id": "1313053895", "x-mock-org-ids": "789,888"},
        )
        assert resp.status_code == 200
        assigned_module_ids = resp.json()["assigned_module_ids"]
        assert len(assigned_module_ids) == 1
        assert assigned_module_ids[0] == str(module_2.id)

        # 4. Assigned modules for user 1313053895 with organization 888 (no assignments)
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1313053895},
            headers={"x-mock-user-id": "1313053895", "x-mock-org-ids": "888"},
        )
        assert resp.status_code == 200
        assert resp.json()["assigned_module_ids"] == []

    async def test_list_users(self, client: AsyncClient) -> None:
        # Fetch the hardcoded users
        resp = await client.get(platform_path("/admin/users"))
        assert resp.status_code == 200
        users = resp.json()

        # Verify total unique users count: 2 AMs, 14 POs (Abdus Salam, Sobita, Dalim, Shidul, 9 Abdullah Al Faruk, Sajedul), 53 SKs
        # Total unique IDs = 2 + 14 + 53 = 69
        assert len(users) == 69

        # Check specific entries
        roles = {u["role"] for u in users}
        assert roles == {"AM", "PO", "SK"}

        salim_reza = next(u for u in users if u["id"] == 1723477249)
        assert salim_reza["name"] == "MD Salim Reza"
        assert salim_reza["role"] == "AM"
        assert salim_reza["district"] == "Lalmonirhat"

        abdus_salam = next(u for u in users if u["id"] == 1708515793)
        assert abdus_salam["name"] == "Md Abdus Salam"
        assert abdus_salam["role"] == "PO"
        assert abdus_salam["district"] == "Lalmonirhat"
        assert abdus_salam["upazila"] == "Lalmonirhat Sadar"
        assert abdus_salam["parent_id"] == 1723477249

        hosneyara = next(u for u in users if u["id"] == 1313053891)
        assert hosneyara["name"] == "Mst. Hosneyara Begum"
        assert hosneyara["role"] == "SK"
        assert hosneyara["district"] == "Lalmonirhat"
        assert hosneyara["upazila"] == "Lalmonirhat Sadar"
        assert hosneyara["parent_id"] == 1708515793

    async def test_sync_filtering_by_po_assignment(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        module_1 = await _seed_module(db_session, "PO Assigned Module")

        # Assign module 1 to PO user 1708515793 (Md Abdus Salam) as po_sk
        await client.post(
            platform_path("/admin/assignments"),
            json={"module_id": str(module_1.id), "assignment_type": "po_sk", "user_ids": [1708515793]},
        )

        # Assigned modules for SK 1313053891 (Mst. Hosneyara Begum) under PO 1708515793
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1313053891},
            headers={"x-mock-user-id": "1313053891"},
        )
        assert resp.status_code == 200
        assigned_module_ids = resp.json()["assigned_module_ids"]
        assert len(assigned_module_ids) == 1
        assert assigned_module_ids[0] == str(module_1.id)

        # Assigned modules for PO themselves 1708515793
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1708515793},
            headers={"x-mock-user-id": "1708515793"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["assigned_module_ids"]) == 1

        # Assigned modules for SK 1313054034 (ANJALI RANI) under PO 1737213126 (Sobita Rani)
        # Should return 0 modules since the module was assigned to PO 1708515793
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1313054034},
            headers={"x-mock-user-id": "1313054034"},
        )
        assert resp.status_code == 200
        assert resp.json()["assigned_module_ids"] == []

    async def test_sync_filtering_po_individual_non_cascade(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        module_1 = await _seed_module(db_session, "PO Individual Module")

        # Assign module 1 to PO user 1708515793 (Md Abdus Salam) as individual
        await client.post(
            platform_path("/admin/assignments"),
            json={"module_id": str(module_1.id), "assignment_type": "individual", "user_ids": [1708515793]},
        )

        # Assigned modules for PO 1708515793 (should get the module)
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1708515793},
            headers={"x-mock-user-id": "1708515793"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["assigned_module_ids"]) == 1

        # Assigned modules for SK 1313053891 (Mst. Hosneyara Begum) under PO 1708515793 (should NOT get the module)
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1313053891},
            headers={"x-mock-user-id": "1313053891"},
        )
        assert resp.status_code == 200
        assert resp.json()["assigned_module_ids"] == []

    async def test_sync_filtering_geographical(self, client: AsyncClient, db_session: AsyncSession) -> None:
        module_1 = await _seed_module(db_session, "Geo Module")

        # Assign module 1 to upazila "Lalmonirhat Sadar"
        resp = await client.post(
            platform_path("/admin/assignments"),
            json={
                "module_id": str(module_1.id),
                "assignment_type": "geographical",
                "upazilas": ["Lalmonirhat Sadar"],
            },
        )
        assert resp.status_code == 201

        # Verify in list API that upazila is returned
        resp = await client.get(platform_path("/admin/assignments"))
        assert resp.status_code == 200
        assignments = resp.json()
        geo_assignment = next(a for a in assignments if a["assignment_type"] == "geographical")
        assert geo_assignment["upazila"] == "Lalmonirhat Sadar"

        # Assigned modules for PO 1708515793 (Md Abdus Salam) whose upazila is "Lalmonirhat Sadar" (should get it)
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1708515793},
            headers={"x-mock-user-id": "1708515793"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["assigned_module_ids"]) == 1

        # Assigned modules for SK 1313053891 (Mst. Hosneyara Begum) whose upazila is "Lalmonirhat Sadar" (should get it)
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1313053891},
            headers={"x-mock-user-id": "1313053891"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["assigned_module_ids"]) == 1

        # Assigned modules for SK 1313054034 (ANJALI RANI) whose upazila is "Aditmari" (should NOT get it)
        resp = await client.get(
            platform_path("/sync/modules"),
            params={"since": "2020-01-01T00:00:00Z", "user_id": 1313054034},
            headers={"x-mock-user-id": "1313054034"},
        )
        assert resp.status_code == 200
        assert resp.json()["assigned_module_ids"] == []

    async def test_create_assignment_validation(self, client: AsyncClient, db_session: AsyncSession) -> None:
        module_1 = await _seed_module(db_session, "Validation Module")

        # 1. Try assigning po_sk to an SK (1313053891 is SK) - should fail with 400
        resp = await client.post(
            platform_path("/admin/assignments"),
            json={"module_id": str(module_1.id), "assignment_type": "po_sk", "user_ids": [1313053891]},
        )
        assert resp.status_code == 400
        assert "is not a PO" in resp.json()["detail"]

        # 2. Try assigning geographical without upazilas - should fail with 400
        resp = await client.post(
            platform_path("/admin/assignments"),
            json={"module_id": str(module_1.id), "assignment_type": "geographical", "upazilas": []},
        )
        assert resp.status_code == 400
        assert "upazilas must be provided" in resp.json()["detail"]

        # 3. Try assigning with non-existent user ID
        resp = await client.post(
            platform_path("/admin/assignments"),
            json={"module_id": str(module_1.id), "assignment_type": "individual", "user_ids": [999999]},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"]
