"""Celery task entrypoints for background work.

Lives at the top of `platform_service` so `celery_app.include` finds it. Each
task is a thin wrapper around a worker coroutine — the worker module owns
session management, error handling, and DB writes. The wrapper is just a
sync→async adapter for Celery's worker pool.

Worker modules are imported inside task bodies (not at module top) so callers
such as ``draft_pipeline`` can import task callables without pulling in the
full worker import graph at registration time.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from uuid import UUID

from sqlalchemy.exc import DBAPIError, OperationalError

from platform_service.celery_app import celery_app
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
    from platform_service.workers.module_completion_worker import process_module_event_job

    _run(process_module_event_job(payload))


@celery_app.task(
    name="platform.process_training_request_event",
    autoretry_for=(OperationalError, DBAPIError),
    max_retries=3,
    default_retry_delay=60,
)
def process_training_request_event_task(payload: dict) -> None:
    """Create a CHW training request from a ``module_requested`` telemetry event.

    Thin Celery wrapper around ``process_training_request_event_job``. Enqueued
    from ``api/telemetry.py`` independently of the module-completion path.
    """
    from platform_service.workers.training_request_event_worker import (
        process_training_request_event_job,
    )

    _run(process_training_request_event_job(payload))


@celery_app.task(
    name="platform.generate_module_quiz",
    autoretry_for=CELERY_TRANSIENT_ERRORS,
    max_retries=2,
    default_retry_delay=120,
)
def generate_module_quiz_task(
    module_id: str,
    step_id: str | None = None,
    *,
    quiz_size: int | None = None,
) -> None:
    """Post-publish quiz generation. Enqueued by Stage 3 on module publish.

    Failure is non-blocking — the module stays published with zero quiz
    questions until the retry succeeds (or the dashboard triggers a manual
    regenerate). The Celery autoretry catches transient ai-runtime errors;
    permanent errors are logged and the task fails after retries are
    exhausted.
    """
    from platform_service.workers.quiz_generation_worker import generate_quiz_for_module

    parsed_step_id = UUID(step_id) if step_id else None
    _run(generate_quiz_for_module(UUID(module_id), step_id=parsed_step_id, quiz_size=quiz_size))


@celery_app.task(
    name="platform.generate_module_card_search_metadata_batch",
    autoretry_for=CELERY_TRANSIENT_ERRORS,
    max_retries=2,
    default_retry_delay=120,
)
def generate_module_card_search_metadata_batch_task(
    module_id: str,
    card_step_id: str | None = None,
    metadata_step_id: str | None = None,
    embedding_step_id: str | None = None,
    trigger_binding_step_id: str | None = None,
    force: bool = False,
    chain_downstream: bool = True,
) -> None:
    """Generate all card search metadata in one LLM call for one module."""
    from platform_service.workers.card_search_metadata_worker import generate_card_search_metadata_batch

    _run(
        generate_card_search_metadata_batch(
            UUID(module_id),
            card_step_id=UUID(card_step_id) if card_step_id else None,
            metadata_step_id=UUID(metadata_step_id) if metadata_step_id else None,
            embedding_step_id=UUID(embedding_step_id) if embedding_step_id else None,
            trigger_binding_step_id=(UUID(trigger_binding_step_id) if trigger_binding_step_id else None),
            force=bool(force),
            chain_downstream=bool(chain_downstream),
        )
    )


@celery_app.task(
    name="platform.generate_module_search_metadata",
    autoretry_for=CELERY_TRANSIENT_ERRORS,
    max_retries=2,
    default_retry_delay=120,
)
def generate_module_search_metadata_task(
    module_id: str,
    step_id: str | None = None,
    embedding_step_id: str | None = None,
    trigger_binding_step_id: str | None = None,
    chain_downstream: bool = True,
) -> None:
    """Post-publish search metadata generation. Chains trigger binding or embedding."""
    from platform_service.workers.search_metadata_worker import generate_search_metadata_for_module

    parsed_step_id = UUID(step_id) if step_id else None
    parsed_embedding_step_id = UUID(embedding_step_id) if embedding_step_id else None
    parsed_trigger_binding_step_id = UUID(trigger_binding_step_id) if trigger_binding_step_id else None
    _run(
        generate_search_metadata_for_module(
            UUID(module_id),
            step_id=parsed_step_id,
            embedding_step_id=parsed_embedding_step_id,
            trigger_binding_step_id=parsed_trigger_binding_step_id,
            chain_downstream=bool(chain_downstream),
        )
    )


@celery_app.task(
    name="platform.bind_assessment_triggers",
    autoretry_for=CELERY_TRANSIENT_ERRORS,
    max_retries=2,
    default_retry_delay=120,
)
def bind_assessment_triggers_task(
    module_id: str,
    step_id: str | None = None,
    embedding_step_id: str | None = None,
) -> None:
    """Post-publish assessment-due trigger binding. Chains embedding on completion."""
    from platform_service.workers.trigger_binding_worker import bind_assessment_triggers_for_module

    parsed_step_id = UUID(step_id) if step_id else None
    parsed_embedding_step_id = UUID(embedding_step_id) if embedding_step_id else None
    _run(
        bind_assessment_triggers_for_module(
            UUID(module_id),
            step_id=parsed_step_id,
            embedding_step_id=parsed_embedding_step_id,
        )
    )


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
    from platform_service.workers.embedding_worker import generate_embedding_for_module

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
    from platform_service.workers.gap_classification_worker import classify_module_gaps_for_module

    parsed_step_id = UUID(step_id) if step_id else None
    _run(classify_module_gaps_for_module(UUID(module_id), step_id=parsed_step_id))


@celery_app.task(name="platform.generate_source_thumbnail")
def generate_source_thumbnail_task(payload: dict) -> None:
    """Render first-page/frame thumbnail and store in MinIO before extraction.

    Failure is non-blocking — the ingest pipeline proceeds regardless.
    """
    from platform_service.workers.thumbnail_worker import run_thumbnail_job

    _run(run_thumbnail_job(payload))


@celery_app.task(
    name="platform.run_ingest_batch",
    autoretry_for=(OperationalError, DBAPIError, *CELERY_TRANSIENT_ERRORS),
    max_retries=2,
    default_retry_delay=120,
    soft_time_limit=_INGEST_SOFT_TIME_LIMIT,
    time_limit=_INGEST_TIME_LIMIT,
)
def run_ingest_batch_task(payload: dict) -> None:
    """Run v3.3 ingest pipelines for one POST /admin/ingest batch.

    Enqueued after uploads and source_document rows are committed. Optional
    cross-source fusion runs in-process after all per-file pipelines finish.
    """
    from platform_service.workers.ingest_worker import run_ingest_batch_job

    _run(run_ingest_batch_job(payload))


@celery_app.task(name="platform.drain_telemetry_buffer")
def drain_telemetry_buffer_task() -> None:
    """Retry buffered ClickHouse telemetry rows after ingest outages."""
    from platform_service.workers.telemetry_buffer_drain import drain_telemetry_buffer_job

    _run(drain_telemetry_buffer_job())


@celery_app.task(
    name="platform.aggregate_chat_faqs",
    autoretry_for=(OperationalError, DBAPIError, *CELERY_TRANSIENT_ERRORS),
    max_retries=2,
    default_retry_delay=300,
)
def aggregate_chat_faqs_task() -> None:
    """Weekly refresh of ranked chat FAQs from digital_help_used telemetry."""
    from platform_service.workers.chat_faq_worker import aggregate_chat_faqs_job

    _run(aggregate_chat_faqs_job())


@celery_app.task(
    name="platform.refresh_module_demand_summary",
    autoretry_for=(OperationalError, DBAPIError, *CELERY_TRANSIENT_ERRORS),
    max_retries=2,
    default_retry_delay=300,
)
def refresh_module_demand_summary_task() -> None:
    """Daily refresh of the cached admin module-demand summary snapshot."""
    from platform_service.workers.module_demand_summary_worker import (
        refresh_module_demand_summaries_job,
    )

    _run(refresh_module_demand_summaries_job())


@celery_app.task(
    name="platform.aggregate_chat_feedback_summary",
    autoretry_for=(OperationalError, DBAPIError, *CELERY_TRANSIENT_ERRORS),
    max_retries=2,
    default_retry_delay=300,
)
def aggregate_chat_feedback_summary_task() -> None:
    """Weekly refresh of per-tenant chat feedback summaries from telemetry."""
    from platform_service.workers.chat_feedback_summary_worker import (
        aggregate_chat_feedback_summary_job,
    )

    _run(aggregate_chat_feedback_summary_job())
