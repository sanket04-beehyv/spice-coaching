"""Admin configs API tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from platform_service.api.admin_configs import router as admin_configs_router
from platform_service.config import get_settings
from platform_service.deps import get_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(admin_configs_router)
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


@pytest_asyncio.fixture(autouse=True)
async def _wipe_and_seed_data(db_session: AsyncSession) -> AsyncIterator[None]:
    # Seed config before the test starts
    await db_session.execute(
        text(
            "INSERT INTO config_threshold (version, key, value_json, title, description) VALUES "
            "(1, 'quiz_reattempt_validity_days', '30'::jsonb, "
            "'Quiz Reattempt Validity (Days)', "
            "'Configure the number of days from the module assignment date during which users can reattempt a quiz. Users are always allowed their first quiz attempt, even if this period has expired. After the first attempt, reattempts are permitted only until the configured validity period ends.') "
            "ON CONFLICT (key) DO NOTHING"
        )
    )
    await db_session.commit()

    yield

    # Clean up after the test
    await db_session.rollback()
    await db_session.execute(text("TRUNCATE config_threshold RESTART IDENTITY CASCADE"))
    await db_session.commit()


class TestAdminConfigsRoutes:
    async def test_list_configs(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/configs"))
        assert resp.status_code == 200
        data = resp.json()
        # Verify the seeded config exists
        keys = [item["key"] for item in data]
        assert "quiz_reattempt_validity_days" in keys

        # Find the specific config and check default value
        duration_config = next(item for item in data if item["key"] == "quiz_reattempt_validity_days")
        assert duration_config["value_json"] == 30
        assert duration_config["title"] == "Quiz Reattempt Validity (Days)"
        assert duration_config["version"] == 1

    async def test_get_config_by_key(self, client: AsyncClient) -> None:
        # Retrieve the seeded config by its key
        resp = await client.get(platform_path("/admin/configs/quiz_reattempt_validity_days"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "quiz_reattempt_validity_days"
        assert data["value_json"] == 30
        assert data["title"] == "Quiz Reattempt Validity (Days)"
        assert data["version"] == 1

        # Retrieval of a non-existent key should return 404
        resp_missing = await client.get(platform_path("/admin/configs/non_existent_key"))
        assert resp_missing.status_code == 404

    async def test_update_config(self, client: AsyncClient) -> None:
        # Update seeded config
        resp = await client.put(
            platform_path("/admin/configs/quiz_reattempt_validity_days"),
            json={"value_json": 45, "title": "Updated Title", "description": "Updated description"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "quiz_reattempt_validity_days"
        assert data["value_json"] == 45
        assert data["title"] == "Updated Title"
        assert data["description"] == "Updated description"
        assert data["version"] == 2

        # Updating a non-existent key should return 404
        resp_missing = await client.put(
            platform_path("/admin/configs/non_existent_key"), json={"value_json": 12}
        )
        assert resp_missing.status_code == 404
