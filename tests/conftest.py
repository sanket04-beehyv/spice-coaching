"""Shared pytest fixtures.

Database-backed tests ("requires_db" marker) require a Postgres reachable via
DATABASE_URL_TEST env var. If it's not set, those tests are skipped — the
unit suite still runs cleanly without any external services.

Spin up the test DB locally:
    docker run -d --name coaching-platform-db-test -p 5433:5432 \\
        -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres \\
        -e POSTGRES_DB=microcoaching pgvector/pgvector:pg15
    export DATABASE_URL_TEST=postgresql+asyncpg://postgres:postgres@localhost:5433/microcoaching

Loop / engine isolation:
    pytest-asyncio (`auto` mode) runs each async test on a fresh event loop.
    asyncpg connections are loop-affine, so the platform's `db.base` engine
    cache must not be reused across tests. We:
      1. Force `DATABASE_URL` to match `DATABASE_URL_TEST` early, so any
         `get_settings()` call sees the test URL.
      2. Have `db_session` route through the platform's own `SessionLocal`,
         so the orchestrator's session and any cache writes (which also use
         `SessionLocal`) share the same per-loop engine + connection pool.
      3. Call `reset_engine_caches()` after each test so the new loop in the
         next test starts with no stale per-loop entries.
"""

import asyncio as _asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from platform_service.config import get_settings
from platform_service.db.base import SessionLocal, get_engine, reset_engine_caches
from sqlalchemy import text as _text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

# Serialize TRUNCATE across test connections — concurrent AccessExclusiveLock
# acquisition deadlocks when SessionLocal / wipe fixtures overlap.
_WIPE_ADVISORY_LOCK_KEY = 874_201_337


async def truncate_tables(session: AsyncSession, tables_csv: str, *, attempts: int = 8) -> None:
    """TRUNCATE ``tables_csv`` under an advisory lock with deadlock retries."""
    await session.rollback()
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            await session.execute(_text(f"SELECT pg_advisory_lock({_WIPE_ADVISORY_LOCK_KEY})"))
            try:
                await session.execute(_text(f"TRUNCATE {tables_csv} RESTART IDENTITY CASCADE"))
                await session.commit()
            finally:
                await session.execute(_text(f"SELECT pg_advisory_unlock({_WIPE_ADVISORY_LOCK_KEY})"))
                await session.commit()
            return
        except DBAPIError as exc:
            last_exc = exc
            await session.rollback()
            await _asyncio.sleep(0.05 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def _has_test_db() -> bool:
    return bool(os.environ.get("DATABASE_URL_TEST"))


requires_db = pytest.mark.skipif(
    not _has_test_db(),
    reason="DATABASE_URL_TEST not set; skipping DB-backed test (set it to a Postgres url to run)",
)


def platform_path(path: str) -> str:
    """Full HTTP path including the platform API root prefix."""
    root = get_settings().api_root_path_normalized
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{root}{path}"


@pytest.fixture(scope="session", autouse=True)
def _disable_spice_auth_for_tests() -> Iterator[None]:
    """API tests assume open access unless they explicitly exercise auth."""
    os.environ["SPICE_AUTH_ENABLED"] = "false"
    try:
        get_settings.cache_clear()
    except Exception:
        pass
    yield


@pytest.fixture(scope="session", autouse=True)
def _align_database_url_with_test_url() -> Iterator[None]:
    """Make sure `settings.database_url` points at the test DB before any
    code calls `get_settings()`. Without this, `SessionLocal` would build
    an engine against a different URL than the conftest fixtures, and
    cache writes during a test would land in the wrong DB (or fail to
    connect at all).
    """
    test_url = os.environ.get("DATABASE_URL_TEST")
    if test_url:
        # Take precedence over any inherited DATABASE_URL.
        os.environ["DATABASE_URL"] = test_url
        # Default test DB password matches the docker container we
        # document above. Honour an explicit override if set.
        os.environ.setdefault("DATABASE_PASSWORD", "postgres")
        # Clear any previously-cached settings.
        try:
            get_settings.cache_clear()
        except Exception:
            pass
    yield


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url = os.environ.get("DATABASE_URL_TEST")
    if not url:
        pytest.skip("DATABASE_URL_TEST not set")
    return url


@pytest.fixture(scope="session", autouse=True)
def _migrate_test_db(_align_database_url_with_test_url: None) -> Iterator[None]:
    """Run alembic upgrade head against the test DB once per session.

    Skip if DATABASE_URL_TEST is not set.
    """
    url = os.environ.get("DATABASE_URL_TEST")
    if not url:
        yield
        return

    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(
        ["uv", "run", "alembic", "-c", "infra/alembic.ini", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\nstdout={result.stdout}\nstderr={result.stderr}")

    # Truncate all data tables (preserve schema and alembic_version) so the
    # session starts clean. Prior-run data left committed by tests that did
    # `await session.commit()` would otherwise leak across runs and pollute
    # search-result + count-by-default assertions.
    async def _truncate_all() -> None:
        engine = _create_async_engine(url, echo=False)
        try:
            async with engine.begin() as conn:
                # Get every table name in the public schema except alembic_version.
                rows = await conn.execute(
                    _text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname='public' AND tablename != 'alembic_version'"
                    )
                )
                tables = [r[0] for r in rows.fetchall()]
                if tables:
                    quoted = ", ".join(f'"{t}"' for t in tables)
                    await conn.execute(_text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
        finally:
            await engine.dispose()

    _asyncio.run(_truncate_all())
    yield


@pytest_asyncio.fixture
async def db_session(test_db_url: str) -> AsyncIterator[AsyncSession]:
    """Per-test async session bound to the platform's `SessionLocal`.

    Routes through `platform_service.db.base` so this fixture and any
    application code under test share the *same* per-loop engine. That
    matters specifically for the cache service: `LlmCallCacheService.put`
    writes via `SessionLocal`, and we want both writes and reads to go
    through one connection pool inside a single test loop.

    Cleanup:
      1. Rollback the session (drop any uncommitted state from the test).
      2. Dispose the per-loop engine so the next loop's id (which may
         re-use the same int) doesn't pick up a stale engine bound to
         a now-closed loop.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        try:
            await session.rollback()
        except Exception:
            pass
        await session.close()
        # Dispose the engine bound to THIS loop and drop all cache entries.
        # The next test runs on a fresh loop and will create a fresh engine.
        engine = get_engine()
        await engine.dispose()
        reset_engine_caches()


@pytest.fixture(autouse=True)
def mock_prompt_templates(monkeypatch: pytest.MonkeyPatch):
    """Stub DB-backed prompt rendering for unit tests without seeded templates."""
    from uuid import uuid4

    from platform_service.services.prompt_template_service import PromptTemplateService, RenderedPrompt

    async def _render(
        self: PromptTemplateService,
        session: AsyncSession | None,
        *,
        template_id: str,
        variant_key: str | None,
        variables: dict[str, str],
    ) -> RenderedPrompt:
        return RenderedPrompt(
            template_id=template_id,
            template_version=1,
            prompt_template_id=uuid4(),
            resolved_system_prompt=f"system:{template_id}",
            resolved_human_message=f"human:{template_id}",
        )

    monkeypatch.setattr(PromptTemplateService, "render", _render)
