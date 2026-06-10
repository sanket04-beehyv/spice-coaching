"""Unit tests for mc_foundation shared helpers."""

from __future__ import annotations

import logging

from mc_foundation.logging import setup_logging
from mc_foundation.request_middleware import REQUEST_ID_HEADER, RequestIdMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient


async def _ok(_request: Request) -> Response:
    return Response("ok", media_type="text/plain")


class TestSetupLogging:
    def test_second_call_is_idempotent(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        if hasattr(root, "_mc_configured"):
            delattr(root, "_mc_configured")

        setup_logging(service_name="test-a", log_level="INFO", json_logs=False, app_env="test")
        handler_count = len(root.handlers)
        setup_logging(service_name="test-b", log_level="DEBUG", json_logs=True, app_env="prod")
        assert len(root.handlers) == handler_count


class TestRequestIdMiddleware:
    def test_echoes_request_id_header(self) -> None:
        app = Starlette(
            routes=[Route("/", _ok)],
            middleware=[Middleware(RequestIdMiddleware)],
        )
        client = TestClient(app)
        response = client.get("/", headers={REQUEST_ID_HEADER: "req-abc-123"})
        assert response.headers[REQUEST_ID_HEADER] == "req-abc-123"

    def test_generates_request_id_when_missing(self) -> None:
        app = Starlette(
            routes=[Route("/", _ok)],
            middleware=[Middleware(RequestIdMiddleware)],
        )
        client = TestClient(app)
        response = client.get("/")
        assert REQUEST_ID_HEADER in response.headers
        assert response.headers[REQUEST_ID_HEADER]
