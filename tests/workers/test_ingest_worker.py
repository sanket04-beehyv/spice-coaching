"""Tests for ingest batch worker thumbnail ordering."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from platform_service.workers.ingest_worker import IngestJob, run_ingest_batch_job


@pytest.mark.asyncio
async def test_batch_continues_when_thumbnail_task_fails() -> None:
    job = IngestJob(
        source_document_id=uuid4(),
        source_path="medtronics-storage/ingest/x.pdf",
        source_type="pdf",
        primary_language="bn",
    )
    async_result = MagicMock()
    async_result.get.side_effect = TimeoutError("thumbnail timed out")

    with (
        patch("platform_service.celery_tasks.generate_source_thumbnail_task") as mock_thumb_task,
        patch(
            "platform_service.workers.ingest_worker.run_pipeline_for_source_job",
            new_callable=AsyncMock,
        ) as mock_pipeline,
        patch(
            "platform_service.workers.ingest_worker.run_cross_source_fusion_job",
            new_callable=AsyncMock,
        ),
    ):
        mock_thumb_task.apply_async.return_value = async_result
        await run_ingest_batch_job(
            {
                "jobs": [
                    {
                        "source_document_id": str(job.source_document_id),
                        "source_path": job.source_path,
                        "source_type": job.source_type,
                        "primary_language": job.primary_language,
                        "skip_merge": False,
                    }
                ],
                "fuse_sources": False,
            }
        )

    mock_thumb_task.apply_async.assert_called_once()
    mock_pipeline.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_waits_for_thumbnail_before_pipeline() -> None:
    job = IngestJob(
        source_document_id=uuid4(),
        source_path="medtronics-storage/ingest/x.pdf",
        source_type="pdf",
        primary_language="bn",
    )
    call_order: list[str] = []
    async_result = MagicMock()

    def record_get(*_args, **_kwargs):
        call_order.append("thumbnail_get")
        return None

    async def record_pipeline(_job: IngestJob) -> None:
        call_order.append("pipeline")

    async_result.get.side_effect = record_get

    with (
        patch("platform_service.celery_tasks.generate_source_thumbnail_task") as mock_thumb_task,
        patch(
            "platform_service.workers.ingest_worker.run_pipeline_for_source_job",
            side_effect=record_pipeline,
        ),
        patch(
            "platform_service.workers.ingest_worker.run_cross_source_fusion_job",
            new_callable=AsyncMock,
        ),
    ):
        mock_thumb_task.apply_async.return_value = async_result
        await run_ingest_batch_job(
            {
                "jobs": [
                    {
                        "source_document_id": str(job.source_document_id),
                        "source_path": job.source_path,
                        "source_type": job.source_type,
                        "primary_language": job.primary_language,
                        "skip_merge": False,
                    }
                ],
                "fuse_sources": False,
            }
        )

    assert call_order == ["thumbnail_get", "pipeline"]
