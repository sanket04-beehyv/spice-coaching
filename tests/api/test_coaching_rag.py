"""Coaching RAG route tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient
from mc_contracts.coaching_rag import CoachingRagResponse
from mc_foundation.problem import register_problem_handlers
from platform_service.api.coaching_rag import router as coaching_rag_router
from platform_service.config import get_settings
from platform_service.deps import get_ai_client, get_db, get_object_storage_client
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import platform_path, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


class _FakeStorage:
    bucket_name = "medtronics-storage"
    allowed_prefixes = frozenset()


@pytest_asyncio.fixture
async def app(db_session: AsyncSession) -> AsyncIterator[FastAPI]:
    app_obj = FastAPI()
    register_problem_handlers(
        app_obj,
        validation_error_type=RequestValidationError,
        http_exception_type=HTTPException,
    )
    api_router = APIRouter(prefix=get_settings().api_root_path_normalized)
    api_router.include_router(coaching_rag_router)
    app_obj.include_router(api_router)

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    ai_mock = MagicMock()
    ai_mock.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    ai_mock.generate = AsyncMock()

    app_obj.dependency_overrides[get_db] = _override_get_db
    app_obj.dependency_overrides[get_ai_client] = lambda: ai_mock
    app_obj.dependency_overrides[get_object_storage_client] = lambda: _FakeStorage()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "platform_service.services.coaching_rag_service.CoachingRagService.query",
            AsyncMock(
                return_value=CoachingRagResponse(
                    answer="উত্তর",
                    retrieved_modules=[],
                    source_documents=[],
                    model="test-model",
                    suggested_questions=["পরবর্তী প্রশ্ন?"],
                )
            ),
        )
        yield app_obj
    app_obj.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestCoachingRagRoute:
    async def test_rag_query_invokes_service(self, client: AsyncClient) -> None:
        resp = await client.post(
            platform_path("/coaching/rag-query"),
            json={"question": "ANC visit steps?"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "উত্তর"
        assert data["suggested_questions"] == ["পরবর্তী প্রশ্ন?"]
