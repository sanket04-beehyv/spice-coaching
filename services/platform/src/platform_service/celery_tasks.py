"""Celery task entrypoints for background work.

Lives at the top of `platform_service` so `celery_app.include` finds it. Each
task is a thin wrapper around a worker coroutine — the worker module owns
session management, error handling, and DB writes. The wrapper is just a
sync→async adapter for Celery's worker pool.
"""

from __future__ import annotations

import asyncio
import logging
import threading

from sqlalchemy.exc import DBAPIError, OperationalError

from platform_service.celery_app import celery_app
from platform_service.workers.embedding_worker import generate_embedding_for_module
from platform_service.workers.gap_classification_worker import classify_module_gaps_for_module
from platform_service.workers.ingest_worker import run_cross_source_fusion_job, run_ingest_batch_job
from platform_service.workers.module_completion_worker import process_module_event_job
from platform_service.workers.quiz_generation_worker import generate_quiz_for_module
from platform_service.workers.telemetry_buffer_drain import drain_telemetry_buffer_job
from platform_service.workers.thumbnail_worker import run_thumbnail_job
from platform_service.workers.transient_errors import CELERY_TRANSIENT_ERRORS

# Multi-file ingest + fusion can run for hours (LLM stages B/C/D).
_INGEST_SOFT_TIME_LIMIT = 2 * 60 * 60
_INGEST_TIME_LIMIT = 4 * 60 * 60

logger = logging.getLogger(__name__)

_CELERY_EVENT_LOOP: asyncio.AbstractEventLoop | None = None
_CELERY_LOOP_THREAD_ID: int | None = None


def _get_celery_loop() -> asyncio.AbstractEventLoop:
    global _CELERY_EVENT_LOOP, _CELERY_LOOP_THREAD_ID
    if _CELERY_EVENT_LOOP is not None:
        return _CELERY_EVENT_LOOP
    loop = asyncio.new_event_loop()
    _CELERY_EVENT_LOOP = loop
    _CELERY_LOOP_THREAD_ID = threading.get_ident()
    return loop


def _run(coro):  # type: ignore[no-untyped-def]
    loop = _get_celery_loop()
    if _CELERY_LOOP_THREAD_ID is not None and _CELERY_LOOP_THREAD_ID != threading.get_ident():
        raise RuntimeError("Celery loop created on different thread; cannot run coroutine safely")
    return loop.run_until_complete(coro)


@celery_app.task(
    name="platform.process_module_event",
    autoretry_for=(OperationalError, DBAPIError),
    max_retries=3,
    default_retry_delay=60,
)
def process_module_event_task(payload: dict) -> None:
    """W-10 — drive module-level completion + escalation off one telemetry event.

    Thin Celery wrapper around `process_module_event_job` (which owns the DB
    session, gap-state mirroring, and error handling). The handler at
    `api/telemetry.py` enqueues this for every module event so the synchronous
    ClickHouse insert path is not blocked on Postgres.
    """
    _run(process_module_event_job(payload))


@celery_app.task(
    name="platform.generate_module_quiz",
    autoretry_for=CELERY_TRANSIENT_ERRORS,
    max_retries=2,
    default_retry_delay=120,
)
def generate_module_quiz_task(module_id: str, step_id: str | None = None) -> None:
    """Post-publish quiz generation. Enqueued by Stage 3 on module publish.

    Failure is non-blocking — the module stays published with zero quiz
    questions until the retry succeeds (or the dashboard triggers a manual
    regenerate). The Celery autoretry catches transient ai-runtime errors;
    permanent errors are logged and the task fails after retries are
    exhausted.
    """
    from uuid import UUID

    parsed_step_id = UUID(step_id) if step_id else None
    _run(generate_quiz_for_module(UUID(module_id), step_id=parsed_step_id))


@celery_app.task(
    name="platform.generate_module_embedding",
    autoretry_for=CELERY_TRANSIENT_ERRORS,
    max_retries=2,
    default_retry_delay=120,
)
def generate_module_embedding_task(module_id: str, step_id: str | None = None) -> None:
    """Post-publish embedding generation. Enqueued by Stage 3 on module
    publish. Failure is non-blocking — the module is still readable in the
    dashboard via title and full-text search until a later run succeeds.
    """
    from uuid import UUID

    parsed_step_id = UUID(step_id) if step_id else None
    _run(generate_embedding_for_module(UUID(module_id), step_id=parsed_step_id))


@celery_app.task(
    name="platform.classify_module_gaps",
    autoretry_for=CELERY_TRANSIENT_ERRORS,
    max_retries=2,
    default_retry_delay=120,
)
def classify_module_gaps_task(module_id: str, step_id: str | None = None) -> None:
    """Post-publish gap classification. Enqueued by Stage 2-draft after module persist."""
    from uuid import UUID

    parsed_step_id = UUID(step_id) if step_id else None
    _run(classify_module_gaps_for_module(UUID(module_id), step_id=parsed_step_id))


@celery_app.task(name="platform.generate_source_thumbnail")
def generate_source_thumbnail_task(payload: dict) -> None:
    """Render first-page/frame thumbnail and store in MinIO before extraction.

    Failure is non-blocking — the ingest pipeline proceeds regardless.
    """
    _run(run_thumbnail_job(payload))


@celery_app.task(
    name="platform.run_ingest_batch",
    soft_time_limit=_INGEST_SOFT_TIME_LIMIT,
    time_limit=_INGEST_TIME_LIMIT,
)
def run_ingest_batch_task(payload: dict) -> None:
    """Run v3.3 ingest pipelines for one POST /admin/ingest batch.

    Enqueued after uploads and source_document rows are committed. Optional
    cross-source fusion runs in-process after all per-file pipelines finish.
    """
    _run(run_ingest_batch_job(payload))


@celery_app.task(
    name="platform.run_cross_source_fusion",
    soft_time_limit=_INGEST_SOFT_TIME_LIMIT,
    time_limit=_INGEST_TIME_LIMIT,
)
def run_cross_source_fusion_task(payload: dict) -> None:
    """Stage 2b → publish for POST /admin/fusion."""
    _run(run_cross_source_fusion_job(payload))


@celery_app.task(name="platform.drain_telemetry_buffer")
def drain_telemetry_buffer_task() -> None:
    """Retry buffered ClickHouse telemetry rows after ingest outages."""
    _run(drain_telemetry_buffer_job())
