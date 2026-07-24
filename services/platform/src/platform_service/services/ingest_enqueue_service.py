"""Celery enqueue helpers for admin ingest."""

from __future__ import annotations

from platform_service.celery_tasks import generate_source_thumbnail_task, run_ingest_batch_task
from platform_service.config import get_settings
from platform_service.services.ingest_upload_service import IngestedSourceResult
from platform_service.workers.ingest_worker import IngestJob, ingest_job_to_dict


def ingest_job_from_result(
    result: IngestedSourceResult,
    *,
    skip_merge: bool,
) -> IngestJob:
    return IngestJob(
        source_document_id=result.source_document_id,
        source_path=result.stored_path,
        source_type=result.source_type,
        primary_language=get_settings().deployment_primary_locale,
        skip_merge=skip_merge,
    )


def enqueue_thumbnail_and_batch(
    ingested: list[IngestedSourceResult],
    *,
    skip_merge: bool,
    fuse_sources: bool,
) -> None:
    for result in ingested:
        generate_source_thumbnail_task.delay(
            ingest_job_to_dict(
                ingest_job_from_result(
                    result,
                    skip_merge=skip_merge,
                )
            )
        )

    run_ingest_batch_task.delay(
        {
            "jobs": [
                ingest_job_to_dict(
                    ingest_job_from_result(
                        result,
                        skip_merge=skip_merge,
                    )
                )
                for result in ingested
            ],
            "fuse_sources": fuse_sources,
        }
    )
