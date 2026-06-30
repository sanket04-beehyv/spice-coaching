"""FastAPI dependency injectors for the platform service."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.config import get_settings
from platform_service.db.base import SessionLocal, dispose_all_engines
from platform_service.integrations.ai_runtime_client import AIRuntimeClient
from platform_service.integrations.spice_auth_client import SpiceAuthClient
from platform_service.services.object_storage import ObjectStorageClient

# Shared client instances — closed on application shutdown via shutdown_clients().
_ai_client: AIRuntimeClient | None = None
_spice_auth_client: SpiceAuthClient | None = None
_object_storage_client: ObjectStorageClient | None = None
_clickhouse_client = None
_redis_client: Redis | None = None


def get_ai_client() -> AIRuntimeClient:
    global _ai_client
    if _ai_client is None:
        _ai_client = AIRuntimeClient()
    return _ai_client


def get_spice_auth_client() -> SpiceAuthClient:
    global _spice_auth_client
    if _spice_auth_client is None:
        _spice_auth_client = SpiceAuthClient()
    return _spice_auth_client


def get_object_storage_client() -> ObjectStorageClient:
    global _object_storage_client
    if _object_storage_client is None:
        _object_storage_client = ObjectStorageClient.from_settings()
    return _object_storage_client


def get_redis_client() -> Redis:
    """Return the process-scoped Redis client (decode_responses=True)."""
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def get_clickhouse_client():
    """Return the process-scoped ClickHouse client."""
    global _clickhouse_client
    if _clickhouse_client is None:
        _clickhouse_client = ClickHouseClient()
    return _clickhouse_client


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def shutdown_clients() -> None:
    """Release HTTP, object-storage, ClickHouse, Redis, and DB pool resources."""
    global _ai_client, _spice_auth_client, _object_storage_client, _clickhouse_client, _redis_client
    if _ai_client is not None:
        await _ai_client.aclose()
        _ai_client = None
    if _spice_auth_client is not None:
        await _spice_auth_client.aclose()
        _spice_auth_client = None
    if _object_storage_client is not None:
        _object_storage_client.close()
        _object_storage_client = None
    if _clickhouse_client is not None:
        _clickhouse_client.close()
        _clickhouse_client = None
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
    await dispose_all_engines()
