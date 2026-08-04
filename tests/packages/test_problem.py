"""Unit tests for RFC 7807 Problem Details helpers."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mc_contracts.errors import ErrorCode
from mc_foundation.problem import (
    PROBLEM_CONTENT_TYPE,
    AppError,
    build_problem_body,
    parse_problem_body,
    register_problem_handlers,
)
from pydantic import BaseModel


class _Body(BaseModel):
    run_id: str


def _app() -> FastAPI:
    app = FastAPI()
    register_problem_handlers(
        app,
        validation_error_type=RequestValidationError,
        http_exception_type=HTTPException,
    )

    @app.get("/boom")
    async def boom() -> None:
        raise AppError(ErrorCode.BATCH_NOT_FOUND.value, "ingest batch not found", status=404)

    @app.get("/legacy")
    async def legacy() -> None:
        raise HTTPException(status_code=400, detail="legacy string")

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret stack")

    @app.post("/validate")
    async def validate(_body: _Body) -> dict[str, str]:
        return {"ok": "yes"}

    return app


class TestBuildProblemBody:
    def test_includes_catalog_ref_and_code(self) -> None:
        body = build_problem_body(
            code="batch_not_found",
            detail="ingest batch not found",
            status=404,
            instance="/admin/ingest/batches/x",
        )
        assert body["type"] == "docs/error-codes.json#batch_not_found"
        assert body["code"] == "batch_not_found"
        assert body["status"] == 404
        assert body["title"] == "Batch Not Found"
        assert body["instance"] == "/admin/ingest/batches/x"


class TestParseProblemBody:
    def test_preserves_upstream_code(self) -> None:
        err = parse_problem_body(
            {
                "type": "docs/error-codes.json#empty_transcript",
                "title": "Empty Transcript",
                "status": 422,
                "detail": "provider returned empty transcript",
                "code": "empty_transcript",
            },
            fallback_status=502,
        )
        assert err.code == "empty_transcript"
        assert err.status == 422
        assert err.detail == "provider returned empty transcript"


class TestProblemHandlers:
    def test_app_error_returns_problem_json(self) -> None:
        client = TestClient(_app(), raise_server_exceptions=False)
        response = client.get("/boom")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        body = response.json()
        assert body["code"] == "batch_not_found"
        assert body["type"] == "docs/error-codes.json#batch_not_found"
        assert body["detail"] == "ingest batch not found"
        assert body["instance"] == "/boom"

    def test_http_exception_normalized(self) -> None:
        client = TestClient(_app(), raise_server_exceptions=False)
        response = client.get("/legacy")
        assert response.status_code == 400
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        body = response.json()
        assert body["code"] == "bad_request"
        assert body["detail"] == "legacy string"

    def test_validation_includes_errors_extension(self) -> None:
        client = TestClient(_app(), raise_server_exceptions=False)
        response = client.post("/validate", json={})
        assert response.status_code == 422
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        body = response.json()
        assert body["code"] == "validation_error"
        assert isinstance(body.get("errors"), list)
        assert body["errors"]

    def test_unhandled_is_internal_error_without_stack(self) -> None:
        client = TestClient(_app(), raise_server_exceptions=False)
        response = client.get("/crash")
        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "internal_error"
        assert "secret stack" not in body["detail"]
        assert "RuntimeError" not in body["detail"]
