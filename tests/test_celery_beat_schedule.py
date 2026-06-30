"""Celery Beat schedule invariants."""

from __future__ import annotations

from celery.schedules import crontab
from platform_service.celery_app import celery_app


def test_aggregate_chat_faqs_runs_weekly_sunday_02_utc() -> None:
    entry = celery_app.conf.beat_schedule["aggregate-chat-faqs"]
    assert entry["task"] == "platform.aggregate_chat_faqs"

    schedule = entry["schedule"]
    assert isinstance(schedule, crontab)
    assert schedule.hour == {2}
    assert schedule.minute == {0}
    assert schedule.day_of_week == {0}
