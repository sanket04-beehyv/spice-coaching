"""ai-runtime FastAPI application.

Run as:
    uvicorn ai_runtime.main:app --host 0.0.0.0 --port 8001

Stateless: no database, no Redis, no ClickHouse.
Only dependency: AI provider API keys (Google Gemini, optionally OpenAI).
"""

from __future__ import annotations

# Load .env before any module-level config reads. Pydantic-settings handles
# declared fields, but the Google SDK looks up GOOGLE_APPLICATION_CREDENTIALS
# directly from os.environ — this ensures it's populated.
import os
from contextlib import asynccontextmanager
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
_ = os  # keep import alive for downstream use

from fastapi import FastAPI  # noqa: E402
from mc_foundation.logging import setup_logging  # noqa: E402
from mc_foundation.request_middleware import RequestIdMiddleware  # noqa: E402

from ai_runtime.api.internal_embed import router as embed_router  # noqa: E402
from ai_runtime.api.internal_generate import router as generate_router  # noqa: E402
from ai_runtime.api.internal_transcribe import router as transcribe_router  # noqa: E402
from ai_runtime.config import get_settings  # noqa: E402
from ai_runtime.services.prompt_executor import close_providers  # noqa: E402


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    await close_providers()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(
        service_name=settings.log_service_name or settings.app_name,
        log_level=settings.log_level,
        json_logs=settings.log_json,
        app_env=settings.app_env,
    )

    app = FastAPI(
        title="MicroCoaching AI Runtime",
        version="0.1.0",
        # Internal service — no public docs
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.add_middleware(RequestIdMiddleware, service_name=settings.app_name)

    app.include_router(generate_router)
    app.include_router(embed_router)
    app.include_router(transcribe_router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": settings.app_name, "provider": settings.ai_provider}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001)
