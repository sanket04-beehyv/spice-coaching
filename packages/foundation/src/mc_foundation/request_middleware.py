"""ASGI middleware for request correlation IDs."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from mc_foundation.tracing import log_request_end, log_request_start, new_request_id

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request ID to every HTTP request and echo it in the response."""

    def __init__(self, app, *, service_name: str = "microcoaching") -> None:
        super().__init__(app)
        self._service_name = service_name

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming.strip() if incoming and incoming.strip() else new_request_id()
        request.state.request_id = request_id
        endpoint = request.url.path
        started = time.monotonic()
        log_request_start(request_id, self._service_name, endpoint)
        response = await call_next(request)
        latency_ms = int((time.monotonic() - started) * 1000)
        log_request_end(request_id, self._service_name, endpoint, response.status_code, latency_ms)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
