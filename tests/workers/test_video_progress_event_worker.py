"""Unit tests for video_progress_event_worker payload parsing / upsert."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from platform_service.workers.video_progress_event_worker import process_video_progress_event_job

pytestmark = [pytest.mark.asyncio]


async def test_worker_upserts_valid_payload() -> None:
    video_id = uuid4()
    payload = {
        "chw_id": 42,
        "tenant_id": None,
        "event_id": str(uuid4()),
        "event_type": "video_progress_updated",
        "payload_json": {
            "source_document_id": str(video_id),
            "last_position_ms": 9_000,
            "percent_watched": 33.0,
            "completed": True,
        },
    }
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.commit = AsyncMock()

    repo = MagicMock()
    repo.upsert = AsyncMock(
        return_value=MagicMock(percent_watched=33.0, completed=True),
    )

    with (
        patch("platform_service.workers.video_progress_event_worker.SessionLocal", return_value=session),
        patch(
            "platform_service.workers.video_progress_event_worker.VideoProgressRepository",
            return_value=repo,
        ),
    ):
        await process_video_progress_event_job(payload)

    repo.upsert.assert_awaited_once()
    kwargs = repo.upsert.await_args.kwargs
    assert kwargs["chw_id"] == 42
    assert kwargs["source_document_id"] == video_id
    assert kwargs["last_position_ms"] == 9_000
    assert kwargs["percent_watched"] == 33.0
    assert kwargs["completed"] is True
    session.commit.assert_awaited_once()


async def test_worker_noops_on_missing_source_document_id() -> None:
    payload = {
        "chw_id": 42,
        "event_id": str(uuid4()),
        "payload_json": {"last_position_ms": 1, "percent_watched": 1.0},
    }
    with patch("platform_service.workers.video_progress_event_worker.VideoProgressRepository") as repo_cls:
        await process_video_progress_event_job(payload)
    repo_cls.assert_not_called()
