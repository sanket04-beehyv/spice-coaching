"""Admin prompts API tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from mc_foundation.problem import register_problem_handlers
from platform_service.api.admin_prompts import router as admin_prompts_router
from platform_service.config import get_settings
from platform_service.deps import get_db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]

_SEED_ID = "00000000-0000-4000-8000-000000000001"


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    register_problem_handlers(
        app_obj,
        validation_error_type=RequestValidationError,
        http_exception_type=HTTPException,
    )
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(admin_prompts_router)
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
async def _seed_prompt(db_session: AsyncSession) -> AsyncIterator[None]:
    await db_session.execute(
        text(
            """
            INSERT INTO prompt_template (
                id, template_id, version, variant_key, generation_type,
                system_prompt_template, human_message_template, required_variables,
                title, description, change_notes, status
            ) VALUES (
                :id, 'test-admin-prompt', 1, NULL, 'module_gap_classification',
                'System {max_associations}', 'Human {module_payload_json}',
                '["max_associations", "module_payload_json"]'::jsonb,
                'Test prompt', 'For admin API tests', 'seed', 'active'
            )
            ON CONFLICT ON CONSTRAINT uq_prompt_template_id_variant_version DO NOTHING
            """
        ),
        {"id": UUID(_SEED_ID)},
    )
    await db_session.commit()
    yield
    await db_session.rollback()
    await db_session.execute(text("DELETE FROM prompt_template WHERE template_id = 'test-admin-prompt'"))
    await db_session.commit()


class TestAdminPromptsRoutes:
    async def test_list_catalog(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/prompts"))
        assert resp.status_code == 200
        data = resp.json()
        assert any(item["template_id"] == "test-admin-prompt" for item in data)

    async def test_list_versions(self, client: AsyncClient) -> None:
        resp = await client.get(platform_path("/admin/prompts/test-admin-prompt"))
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["version"] == 1
        assert data[0]["status"] == "active"

    async def test_create_and_activate_version(self, client: AsyncClient) -> None:
        create = await client.post(
            platform_path("/admin/prompts/test-admin-prompt/versions"),
            json={
                "system_prompt_template": "Updated system {max_associations}",
                "human_message_template": "Updated human {module_payload_json}",
                "required_variables": ["max_associations", "module_payload_json"],
                "change_notes": "operator edit",
            },
        )
        assert create.status_code == 200
        created = create.json()
        assert created["version"] == 2
        assert created["status"] == "deprecated"

        activate = await client.post(
            platform_path("/admin/prompts/test-admin-prompt/versions/2/activate"),
            json={},
        )
        assert activate.status_code == 200
        activated = activate.json()
        assert activated["status"] == "active"
        assert activated["version"] == 2

    async def test_preview(self, client: AsyncClient) -> None:
        resp = await client.post(
            platform_path("/admin/prompts/test-admin-prompt/preview"),
            json={
                "variables": {
                    "max_associations": "3",
                    "module_payload_json": "{}",
                }
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "System 3" in data["resolved_system_prompt"]
