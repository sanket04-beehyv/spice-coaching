"""Celery application configuration for platform_service.

Redis is used as the broker. We intentionally do not configure a Celery result
backend — task outcomes are observable via the rows the workers write (e.g.
`module.lifecycle_status`, `ingestion_run_step.status`). Polling a result
backend on top of that is duplication.

Task registry: `celery_tasks.py` (the `include` argument below). Beat schedule
entries are added per-task as they come online (e.g. embedding/quiz post-publish
workers). The post-publish jobs are event-triggered, not scheduled, so the beat
schedule is empty by default.
"""

from __future__ import annotations

import asyncio
import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init, worker_process_shutdown

from platform_service.config import get_settings
from platform_service.db.base import dispose_all_engines, reset_engine_caches
from platform_service.deps import shutdown_clients

logger = logging.getLogger(__name__)


def create_celery_app() -> Celery:
    settings = get_settings()

    app = Celery(
        "platform_service",
        broker=settings.redis_url,
        include=["platform_service.celery_tasks"],
    )

    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        enable_utc=True,
        timezone="UTC",
        task_track_started=True,
        beat_schedule={
            "drain-telemetry-buffer": {
                "task": "platform.drain_telemetry_buffer",
                "schedule": float(settings.telemetry_buffer_drain_interval_seconds),
            },
            "aggregate-chat-faqs": {
                "task": "platform.aggregate_chat_faqs",
                "schedule": crontab(
                    hour=settings.chat_faq_weekly_hour_utc,
                    minute=0,
                    day_of_week=settings.chat_faq_weekly_day_of_week,
                ),
            },
            "aggregate-chat-feedback-summary": {
                "task": "platform.aggregate_chat_feedback_summary",
                "schedule": crontab(
                    hour=settings.chat_feedback_summary_weekly_hour_utc,
                    minute=0,
                    day_of_week=settings.chat_feedback_summary_weekly_day_of_week,
                ),
            },
            "refresh-module-demand-summary": {
                "task": "platform.refresh_module_demand_summary",
                "schedule": crontab(
                    hour=settings.module_demand_summary_daily_hour_utc,
                    minute=0,
                ),
            },
        },
    )

    return app


celery_app = create_celery_app()


@worker_process_init.connect
def _on_worker_process_init(**_kwargs: object) -> None:
    """Post-fork: configure logging, dispose inherited SQLAlchemy pools, reset engine caches."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(dispose_all_engines())
    finally:
        loop.close()
    reset_engine_caches()
    logger.debug("Celery worker process init: engine pools disposed and caches reset")


@worker_process_shutdown.connect
def _on_worker_process_shutdown(**_kwargs: object) -> None:
    """Release httpx pools and DB engines before the worker child exits."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(shutdown_clients())
    finally:
        loop.close()
    logger.debug("Celery worker process shutdown: shared clients closed")
