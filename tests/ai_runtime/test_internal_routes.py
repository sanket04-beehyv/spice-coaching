"""HTTP tests for ai-runtime internal generate and embed routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from ai_runtime.config import get_settings
from ai_runtime.main import create_app
from httpx import ASGITransport, AsyncClient
from mc_contracts.enums import GenerationType
from mc_contracts.internal_ai import (
    GenerationConstraints,
    InferenceRequest,
    InferenceResponse,
    PromptSpec,
    TokenUsage,
)


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Token": get_settings().internal_token}


def _sample_request(*, generation_type: GenerationType = GenerationType.QUIZ_DRAFTING) -> InferenceRequest:
    return InferenceRequest(
        request_id="req-route-1",
        generation_type=generation_type,
        prompt=PromptSpec(
            template_id="t",
            template_version=1,
            resolved_system_prompt="sys",
            resolved_human_message="human",
        ),
        constraints=GenerationConstraints(language="bn", output_format="json"),
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestGenerateRoute:
    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self, client: AsyncClient) -> None:
        body = _sample_request().model_dump(mode="json")
        resp = await client.post("/internal/generate/quiz_drafting", json=body)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unknown_generation_type_returns_400(self, client: AsyncClient) -> None:
        body = _sample_request().model_dump(mode="json")
        resp = await client.post(
            "/internal/generate/not-a-type",
            json=body,
            headers=_internal_headers(),
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "bad_request"
        assert "Unknown generation_type" in body["detail"]
        assert resp.headers["content-type"].startswith("application/problem+json")

    @pytest.mark.asyncio
    async def test_path_body_mismatch_returns_400(self, client: AsyncClient) -> None:
        body = _sample_request(generation_type=GenerationType.QUIZ_DRAFTING).model_dump(mode="json")
        resp = await client.post(
            "/internal/generate/card_drafting",
            json=body,
            headers=_internal_headers(),
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "bad_request"
        assert "does not match body" in body["detail"]

    @pytest.mark.asyncio
    async def test_success_delegates_to_executor(self, client: AsyncClient) -> None:
        expected = InferenceResponse(
            request_id="req-route-1",
            generation_type=GenerationType.QUIZ_DRAFTING,
            provider="google",
            model="gemini-2.5-flash",
            max_tokens=8192,
            temperature=0.2,
            raw_text='{"ok": true}',
            parsed_json={"ok": True},
            latency_ms=5,
            token_usage=TokenUsage(input=3, output=7),
        )
        with patch(
            "ai_runtime.api.internal_generate._executor.execute",
            new_callable=AsyncMock,
            return_value=expected,
        ):
            resp = await client.post(
                "/internal/generate/quiz_drafting",
                json=_sample_request().model_dump(mode="json"),
                headers=_internal_headers(),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["request_id"] == "req-route-1"
        assert data["parsed_json"] == {"ok": True}
        assert data["error"] is None


class TestEmbedRoute:
    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self, client: AsyncClient) -> None:
        resp = await client.post("/internal/embed", json={"texts": ["hello"]})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_texts_returns_empty_embeddings(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/internal/embed",
            json={"texts": []},
            headers=_internal_headers(),
        )
        assert resp.status_code == 200
        assert resp.json() == {"embeddings": []}

    @pytest.mark.asyncio
    async def test_too_many_texts_returns_400(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/internal/embed",
            json={"texts": ["x"] * 101},
            headers=_internal_headers(),
        )
        assert resp.status_code == 400
        assert "Maximum 100" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_success_returns_embeddings(self, client: AsyncClient) -> None:
        vectors = [[0.1, 0.2], [0.3, 0.4]]
        with patch(
            "ai_runtime.api.internal_embed._executor.embed",
            new_callable=AsyncMock,
            return_value=vectors,
        ):
            resp = await client.post(
                "/internal/embed",
                json={"texts": ["a", "b"]},
                headers=_internal_headers(),
            )
        assert resp.status_code == 200
        assert resp.json() == {"embeddings": vectors}
