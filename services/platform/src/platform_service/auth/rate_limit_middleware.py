"""Redis-backed sliding-window rate limiting for hot endpoints."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from mc_contracts.errors import ErrorCode
from mc_foundation.problem import problem_json_response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from platform_service.config import Settings, get_settings
from platform_service.deps import get_redis_client

logger = logging.getLogger(__name__)

_RATE_LIMIT_RULES: tuple[tuple[str, str], ...] = (
    ("telemetry/events", "telemetry"),
    ("coaching/rag-query", "rag"),
    ("admin/ingest", "admin_ingest"),
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply per-IP sliding-window limits on configured route suffixes."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return await call_next(request)

        rule = _match_rate_limit_rule(request.url.path, settings)
        if rule is None:
            return await call_next(request)

        suffix, bucket = rule
        limit = _limit_for_bucket(settings, bucket)
        if limit <= 0:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        window = 60
        now = int(time.time())
        window_start = now - (now % window)
        key = f"rate:{bucket}:{client_ip}:{window_start}"

        redis = get_redis_client()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window + 1)

        if count > limit:
            logger.warning(
                "rate limit exceeded bucket=%s ip=%s path_suffix=%s count=%d limit=%d",
                bucket,
                client_ip,
                suffix,
                count,
                limit,
            )
            return problem_json_response(
                code=ErrorCode.RATE_LIMIT_EXCEEDED.value,
                detail="rate limit exceeded",
                status=429,
                instance=str(request.url.path),
            )

        return await call_next(request)


def _normalized_path(path: str, settings: Settings) -> str:
    root = settings.api_root_path_normalized
    if root and path.startswith(root):
        return path[len(root) :].lstrip("/")
    return path.lstrip("/")


def _match_rate_limit_rule(path: str, settings: Settings) -> tuple[str, str] | None:
    normalized = _normalized_path(path, settings).lower()
    for suffix, bucket in _RATE_LIMIT_RULES:
        if normalized == suffix or normalized.endswith(f"/{suffix}"):
            return suffix, bucket
    return None


def _limit_for_bucket(settings: Settings, bucket: str) -> int:
    if bucket == "telemetry":
        return settings.rate_limit_telemetry_per_minute
    if bucket == "rag":
        return settings.rate_limit_rag_per_minute
    if bucket == "admin_ingest":
        return settings.rate_limit_admin_ingest_per_minute
    return 0
