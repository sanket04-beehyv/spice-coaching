"""Lightweight request tracing utilities."""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


def new_request_id() -> str:
    """Generate a new UUID4 request ID."""
    return str(uuid.uuid4())


def log_request_start(request_id: str, service: str, endpoint: str) -> None:
    logger.info("request_start", extra={"request_id": request_id, "service": service, "endpoint": endpoint})


def log_request_end(
    request_id: str,
    service: str,
    endpoint: str,
    status: int,
    latency_ms: int,
) -> None:
    logger.info(
        "request_end",
        extra={
            "request_id": request_id,
            "service": service,
            "endpoint": endpoint,
            "status": status,
            "latency_ms": latency_ms,
        },
    )
