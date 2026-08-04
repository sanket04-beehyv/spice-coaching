"""Celery enqueue helpers for admin ingest."""

from __future__ import annotations

from uuid import UUID

from platform_service.celery_tasks import (
    bind_assessment_triggers_task,
    classify_module_gaps_task,
    generate_module_card_search_metadata_batch_task,
    generate_module_embedding_task,
    generate_module_quiz_task,
    generate_module_search_metadata_task,
    generate_source_thumbnail_task,
    retry_ingest_fusion_task,
    retry_ingest_pipeline_task,
    run_ingest_batch_task,
)
from platform_service.config import get_settings
from platform_service.services.ingest_upload_service import IngestedSourceResult
from platform_service.services.run_state_service import (
    STAGE_CARD_SEARCH_METADATA_GENERATION,
    STAGE_EMBEDDING_GENERATION,
    STAGE_GAP_CLASSIFICATION,
    STAGE_QUIZ_GENERATION,
    STAGE_SEARCH_METADATA_GENERATION,
    STAGE_TRIGGER_BINDING,
)
from platform_service.workers.ingest_worker import IngestJob, ingest_job_to_dict


def ingest_job_from_result(
    result: IngestedSourceResult,
    *,
    run_id: UUID,
    batch_id: UUID,
) -> IngestJob:
    return IngestJob(
        source_document_id=result.source_document_id,
        source_path=result.stored_path,
        source_type=result.source_type,
        primary_language=get_settings().deployment_primary_locale,
        run_id=run_id,
        batch_id=batch_id,
    )


def enqueue_thumbnail_and_batch(
    jobs: list[IngestJob],
    *,
    batch_id: UUID,
) -> None:
    for job in jobs:
        generate_source_thumbnail_task.delay(ingest_job_to_dict(job))

    run_ingest_batch_task.delay(
        {
            "batch_id": str(batch_id),
            "jobs": [ingest_job_to_dict(job) for job in jobs],
        }
    )


def enqueue_thumbnail_retry(
    *,
    source_document_id: UUID,
    source_path: str,
    source_type: str,
    run_id: UUID,
) -> None:
    generate_source_thumbnail_task.delay(
        {
            "source_document_id": str(source_document_id),
            "source_path": source_path,
            "source_type": source_type,
            "primary_language": get_settings().deployment_primary_locale,
            "run_id": str(run_id),
        }
    )


def enqueue_pipeline_resume(
    *,
    source_document_id: UUID,
    source_path: str,
    source_type: str,
    primary_language: str,
    run_id: UUID,
    batch_id: UUID,
    identify_chunk_ids: list[str] | None = None,
) -> None:
    payload: dict = {
        "source_document_id": str(source_document_id),
        "source_path": source_path,
        "source_type": source_type,
        "primary_language": primary_language,
        "run_id": str(run_id),
        "batch_id": str(batch_id),
    }
    if identify_chunk_ids:
        payload["identify_chunk_ids"] = list(identify_chunk_ids)
    retry_ingest_pipeline_task.delay(payload)


def enqueue_fusion_retry(
    *,
    source_document_ids: list[UUID],
    batch_id: UUID,
    fusion_run_id: UUID,
) -> None:
    retry_ingest_fusion_task.delay(
        {
            "source_document_ids": [str(d) for d in source_document_ids],
            "batch_id": str(batch_id),
            "fusion_run_id": str(fusion_run_id),
        }
    )


def enqueue_post_publish_step_retry(
    *,
    stage: str,
    module_id: UUID,
    step_id: UUID,
    candidate_id: UUID | None = None,
) -> None:
    """Re-enqueue a single failed post-publish Celery task bound to its step."""
    del candidate_id  # reserved for future quiz_size lookup
    mid = str(module_id)
    sid = str(step_id)
    if stage == STAGE_QUIZ_GENERATION:
        generate_module_quiz_task.delay(mid, sid)
        return
    if stage == STAGE_EMBEDDING_GENERATION:
        generate_module_embedding_task.delay(mid, sid)
        return
    if stage == STAGE_SEARCH_METADATA_GENERATION:
        generate_module_search_metadata_task.delay(mid, sid, None, None, False)
        return
    if stage == STAGE_GAP_CLASSIFICATION:
        classify_module_gaps_task.delay(mid, sid)
        return
    if stage == STAGE_TRIGGER_BINDING:
        bind_assessment_triggers_task.delay(mid, sid)
        return
    if stage == STAGE_CARD_SEARCH_METADATA_GENERATION:
        generate_module_card_search_metadata_batch_task.delay(
            mid,
            sid,
            None,
            None,
            None,
            False,
            False,
        )
        return
    raise ValueError(f"unsupported post-publish stage for retry: {stage!r}")
