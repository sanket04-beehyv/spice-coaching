"""SQLAlchemy async engine, session factory, and declarative base.

Owned exclusively by platform. AI runtime has no access to this module.

asyncpg connections are loop-affine — a connection opened on event loop A
cannot be used from event loop B (raises "Future attached to a different
loop"). We keep a per-loop engine cache (`_engine_by_loop`) so each loop
gets its own engine + connection pool.

The previous implementation also kept a module-level `_engine` global and
fell through to it when the current loop had no cached engine; that caused
cross-loop leakage in pytest (each test runs in a fresh loop) and in any
code path that calls `asyncio.run(...)` more than once. The fix is: only
return an engine that was created *for the current loop*. New loops always
get a fresh engine.

For sync contexts (no running loop — e.g. CLI tools that create their own
loop later), we fall back to a module-level singleton; that path doesn't
trigger the cross-loop problem because by definition there's no loop yet.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from platform_service.config import get_settings

# Per-loop registries. Keyed by id(loop). NOTE: loop ids can be reused after
# loop garbage-collection, so cleanup-aware code (test fixtures) is welcome
# to call `reset_engine_caches()` between loops to avoid stale lookups.
_engine_by_loop: dict[int, AsyncEngine] = {}
_session_maker_by_loop: dict[int, async_sessionmaker[AsyncSession]] = {}

# Sync-context fallback only. Code paths that have a running loop never
# touch this. Tests should not depend on it.
_sync_engine: AsyncEngine | None = None
_sync_session_maker: async_sessionmaker[AsyncSession] | None = None


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    url = make_url(settings.database_url)
    if settings.database_password:
        url = url.set(password=settings.database_password)
    return create_async_engine(
        url,
        echo=False,
        pool_pre_ping=settings.database_pool_pre_ping,
        pool_recycle=settings.database_pool_recycle,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )


def get_engine() -> AsyncEngine:
    """Return an AsyncEngine bound to the current event loop.

    If there is a running loop, returns (or creates) a per-loop engine.
    If there is no running loop (rare — sync entrypoints), returns a
    module-level singleton.
    """
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = None

    if loop_id is not None:
        existing = _engine_by_loop.get(loop_id)
        if existing is not None:
            return existing
        engine = _build_engine()
        _engine_by_loop[loop_id] = engine
        return engine

    # No running loop — sync-context fallback.
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = _build_engine()
    return _sync_engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """Return an async_sessionmaker bound to the current loop's engine."""
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = None

    if loop_id is not None:
        existing = _session_maker_by_loop.get(loop_id)
        if existing is not None:
            return existing
        maker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
        _session_maker_by_loop[loop_id] = maker
        return maker

    global _sync_session_maker
    if _sync_session_maker is None:
        _sync_session_maker = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _sync_session_maker


async def dispose_all_engines() -> None:
    """Dispose every cached engine and release pool connections.

    Call from application shutdown (FastAPI lifespan, Celery worker teardown).
    """
    global _sync_engine, _sync_session_maker
    for engine in list(_engine_by_loop.values()):
        await engine.dispose()
    if _sync_engine is not None:
        await _sync_engine.dispose()
    _engine_by_loop.clear()
    _session_maker_by_loop.clear()
    _sync_engine = None
    _sync_session_maker = None


def reset_engine_caches() -> None:
    """Clear all cached engines + session makers without awaiting dispose.

    Test fixtures should call this between loops to avoid stale entries
    being returned for new loops that happen to receive a recycled
    `id(loop)`. Production code should not need this.
    """
    global _sync_engine, _sync_session_maker
    _engine_by_loop.clear()
    _session_maker_by_loop.clear()
    _sync_engine = None
    _sync_session_maker = None


class _SessionLocalProxy:
    def __call__(self) -> AsyncSession:
        return get_session_maker()()


SessionLocal = _SessionLocalProxy()


class Base(DeclarativeBase):
    pass


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_maker()() as session:
        yield session
