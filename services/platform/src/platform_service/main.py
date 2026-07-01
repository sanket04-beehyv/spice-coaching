"""platform-api FastAPI application entrypoint.

Run as:
    uvicorn platform_service.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

# Load .env before any other import that touches Settings. Pydantic-settings
# handles declared fields, but environment variables consumed by external
# SDKs (notably GOOGLE_APPLICATION_CREDENTIALS for google-genai) are read
# directly from os.environ — this ensures they are populated.
from pathlib import Path

try:
    from dotenv import load_dotenv

    _here = Path(__file__).resolve()
    for _candidate in (_here.parents[4] / ".env", _here.parents[3] / ".env"):
        if _candidate.exists():
            load_dotenv(_candidate, override=False)
            break
except ImportError:
    pass

import logging  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import APIRouter, FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from mc_foundation.logging import setup_logging  # noqa: E402
from mc_foundation.request_middleware import RequestIdMiddleware  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from sqlalchemy import text  # noqa: E402

from platform_service.api.admin_assignments import router as admin_assignments_router
from platform_service.api.admin_files import router as admin_files_router  # noqa: E402
from platform_service.api.admin_ingest import router as admin_ingest_router  # noqa: E402
from platform_service.api.admin_ingestion_runs import router as admin_ingestion_runs_router  # noqa: E402
from platform_service.api.admin_modules import router as admin_modules_router  # noqa: E402
from platform_service.api.admin_trigger_bindings import router as admin_trigger_bindings_router  # noqa: E402
from platform_service.api.coaching_rag import router as coaching_rag_router  # noqa: E402
from platform_service.api.dashboard import router as dashboard_router  # noqa: E402
from platform_service.api.morning import router as morning_router  # noqa: E402
from platform_service.api.sync import router as sync_router  # noqa: E402
from platform_service.api.telemetry import router as telemetry_router  # noqa: E402
from platform_service.auth.rate_limit_middleware import RateLimitMiddleware  # noqa: E402
from platform_service.auth.spice_auth_middleware import SpiceAuthMiddleware  # noqa: E402
from platform_service.auth.spice_authorization_middleware import SpiceAuthorizationMiddleware  # noqa: E402
from platform_service.config import get_settings  # noqa: E402
from platform_service.db.base import SessionLocal  # noqa: E402
from platform_service.deps import (  # noqa: E402
    get_clickhouse_client,
    get_object_storage_client,
    shutdown_clients,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(fastapi_app: FastAPI):
    yield
    await shutdown_clients()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(
        service_name=settings.log_service_name or settings.app_name,
        log_level=settings.log_level,
        json_logs=settings.log_json,
        app_env=settings.app_env,
    )
    clickhouse_client = get_clickhouse_client()
    api_prefix = settings.api_root_path_normalized
    docs_enabled = settings.app_env != "production"

    fastapi_app = FastAPI(
        title="MicroCoaching Platform API",
        version="0.1.0",
        docs_url=f"{api_prefix}/docs" if docs_enabled else None,
        openapi_url=f"{api_prefix}/openapi.json" if docs_enabled else None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    fastapi_app.add_middleware(RequestIdMiddleware, service_name=settings.app_name)
    # Last added runs first on request: rate limit, then auth, then authorization.
    fastapi_app.add_middleware(RateLimitMiddleware)
    fastapi_app.add_middleware(SpiceAuthorizationMiddleware)
    fastapi_app.add_middleware(SpiceAuthMiddleware)

    api_router = APIRouter() if not api_prefix else APIRouter(prefix=api_prefix)
    api_router.include_router(telemetry_router)
    api_router.include_router(coaching_rag_router)
    api_router.include_router(admin_ingest_router)
    api_router.include_router(admin_files_router)
    api_router.include_router(admin_modules_router)
    api_router.include_router(admin_trigger_bindings_router)
    api_router.include_router(admin_ingestion_runs_router)
    api_router.include_router(admin_assignments_router)
    api_router.include_router(dashboard_router)
    api_router.include_router(morning_router)
    api_router.include_router(sync_router)

    @api_router.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": settings.app_name}

    @api_router.get("/ready")
    async def ready() -> dict:
        checks: dict[str, str] = {}

        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            logger.warning("readiness check failed: database", exc_info=True)
            checks["database"] = "error"

        try:
            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            try:
                await redis.ping()
            finally:
                await redis.aclose()
            checks["redis"] = "ok"
        except Exception:
            logger.warning("readiness check failed: redis", exc_info=True)
            checks["redis"] = "error"

        try:
            await clickhouse_client.query_rows("SELECT 1")
            checks["clickhouse"] = "ok"
        except Exception:
            logger.warning("readiness check failed: clickhouse", exc_info=True)
            checks["clickhouse"] = "error"

        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{settings.ai_runtime_base_url.rstrip('/')}/health")
                response.raise_for_status()
                body = response.json()
                if body.get("provider") != settings.ai_cloud_provider:
                    logger.warning(
                        "readiness check failed: ai_runtime provider mismatch (platform=%s ai_runtime=%s)",
                        settings.ai_cloud_provider,
                        body.get("provider"),
                    )
                    checks["ai_runtime"] = "error"
                else:
                    checks["ai_runtime"] = "ok"
        except Exception:
            logger.warning("readiness check failed: ai_runtime", exc_info=True)
            checks["ai_runtime"] = "error"

        try:
            storage = get_object_storage_client()
            await storage.check_readiness()
            checks["object_storage"] = "ok"
        except Exception:
            logger.warning("readiness check failed: object_storage", exc_info=True)
            checks["object_storage"] = "error"

        if any(value != "ok" for value in checks.values()):
            raise HTTPException(status_code=503, detail={"status": "degraded", "checks": checks})

        return {"status": "ok", "service": settings.app_name, "checks": checks}

    fastapi_app.include_router(api_router)

    @fastapi_app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": settings.app_name,
            "api_root": api_prefix,
            "health": f"{api_prefix}/health",
            "docs": f"{api_prefix}/docs" if docs_enabled else "",
        }

    return fastapi_app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
