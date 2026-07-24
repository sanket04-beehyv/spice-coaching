"""Ingest and cross-source fusion background jobs (Celery-backed).

``POST /admin/ingest`` uploads files synchronously, then enqueues
``run_ingest_batch_job``. When ``fuse_sources=true``, cross-source fusion
runs in-process after all per-file pipelines finish. Each job opens its own
DB session(s) — the API request session is not reused.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from platform_service.config import get_settings
from platform_service.db.base import SessionLocal
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.attribution_audit import record_attribution_event
from platform_service.services.cross_source_fusion_runner import CrossSourceFusionRunner
from platform_service.services.run_state_service import (
    RUN_FAILED,
    ConcurrentFusionRunError,
    ConcurrentRunError,
    RunStateService,
)
from platform_service.services.source_thumbnail_service import source_type_supports_thumbnail
from platform_service.workers.pipeline_orchestrator import PipelineOrchestrator

logger = logging.getLogger(__name__)

_THUMBNAIL_POLL_INTERVAL_S = 0.5


@dataclass(frozen=True)
class IngestJob:
    """Pipeline work for one ingested source_document."""

    source_document_id: UUID
    source_path: str
    source_type: str
    primary_language: str
    skip_merge: bool = False


def _ingest_job_from_dict(data: dict[str, Any]) -> IngestJob:
    return IngestJob(
        source_document_id=UUID(str(data["source_document_id"])),
        source_path=str(data["source_path"]),
        source_type=str(data["source_type"]),
        primary_language=str(data["primary_language"]),
        skip_merge=bool(data.get("skip_merge", False)),
    )


def ingest_job_to_dict(job: IngestJob) -> dict[str, Any]:
    return {
        "source_document_id": str(job.source_document_id),
        "source_path": job.source_path,
        "source_type": job.source_type,
        "primary_language": job.primary_language,
        "skip_merge": job.skip_merge,
    }


async def _mark_active_ingest_failed(source_document_id: UUID) -> None:
    """Mark any running ingestion run as failed after an unexpected crash."""
    async with SessionLocal() as session:
        run_state = RunStateService(session)
        active = await run_state.find_active_run(source_document_id)
        if active is not None:
            await run_state.complete_run(
                active.id,
                status=RUN_FAILED,
                error_jsonb={"detail": "pipeline crashed"},
            )
            await session.commit()


async def run_pipeline_for_source_job(job: IngestJob) -> None:
    """Run the full A→B→C→D pipeline for one source_document."""
    logger.info("Running pipeline for source_document_id=%s", job.source_document_id)
    try:
        result = await PipelineOrchestrator.run_staged(
            source_document_id=job.source_document_id,
            source_path=job.source_path,
            source_type=job.source_type,
            primary_language=job.primary_language,
            skip_merge=job.skip_merge,
        )
        async with SessionLocal() as session:
            await record_attribution_event(
                session,
                event_type="ingest_completed",
                actor="system",
                source_document_id=job.source_document_id,
                payload={"final_status": result.final_status, "run_id": str(result.run_id)},
            )
            await session.commit()
        logger.info(
            "Pipeline finished run_id=%s final_status=%s candidates=%d drafts=%d",
            result.run_id,
            result.final_status,
            result.candidates_emitted,
            result.drafts_produced,
        )
    except ConcurrentRunError:
        logger.warning(
            "Skipping ingest for source_document_id=%s — another worker owns the active run",
            job.source_document_id,
        )
        return
    except Exception:
        logger.exception("Pipeline crashed for source_document_id=%s", job.source_document_id)
        try:
            await _mark_active_ingest_failed(job.source_document_id)
        except Exception:
            logger.exception("Failed to mark ingestion run failed for %s", job.source_document_id)
        try:
            async with SessionLocal() as failure_session:
                await record_attribution_event(
                    failure_session,
                    event_type="ingest_failed",
                    actor="system",
                    source_document_id=job.source_document_id,
                    payload={"detail": "pipeline crashed"},
                )
                await failure_session.commit()
        except Exception:
            logger.exception("Failed to record ingest_failed for %s", job.source_document_id)
        raise


async def run_cross_source_fusion_job(payload: dict[str, Any]) -> None:
    """Run Stage 2b → publish for the given source documents."""
    source_document_ids = [UUID(str(d)) for d in payload["source_document_ids"]]
    try:
        summary = await CrossSourceFusionRunner.run_staged(source_document_ids)
        logger.info(
            "Fusion run %s finished: input=%d groups=%d published=%d failed=%d coverage_warnings=%d retired=%d",
            summary.fusion_run_id,
            summary.input_candidate_count,
            summary.fusion_group_count,
            summary.fused_modules_published,
            summary.fused_modules_failed,
            summary.fused_modules_with_coverage_warning,
            summary.constituents_retired,
        )
    except (ConcurrentRunError, ConcurrentFusionRunError) as exc:
        logger.warning(
            "Skipping fusion for source_document_ids=%s — %s",
            [str(d) for d in source_document_ids],
            exc,
        )
    except Exception:
        logger.exception(
            "Fusion run crashed for source_document_ids=%s",
            [str(d) for d in source_document_ids],
        )
        raise


async def _wait_for_thumbnail_ready(job: IngestJob) -> None:
    """Poll for thumbnail_storage_path set by the upload-time Celery task."""
    if not source_type_supports_thumbnail(job.source_type):
        return

    settings = get_settings()
    deadline = time.monotonic() + settings.ingest_thumbnail_wait_seconds
    logger.info(
        "Waiting for thumbnail source_document_id=%s timeout_seconds=%d",
        job.source_document_id,
        settings.ingest_thumbnail_wait_seconds,
    )
    try:
        while time.monotonic() < deadline:
            async with SessionLocal() as session:
                doc = await SourceRepository(session).get_source_document(job.source_document_id)
                if doc is not None and doc.thumbnail_storage_path:
                    logger.info(
                        "Thumbnail ready source_document_id=%s path=%s",
                        job.source_document_id,
                        doc.thumbnail_storage_path,
                    )
                    return
            await asyncio.sleep(_THUMBNAIL_POLL_INTERVAL_S)
        logger.warning(
            "Thumbnail wait timed out source_document_id=%s after %ds",
            job.source_document_id,
            settings.ingest_thumbnail_wait_seconds,
        )
        raise TimeoutError(f"thumbnail did not complete within {settings.ingest_thumbnail_wait_seconds}s")
    except Exception:
        logger.warning(
            "Thumbnail task did not complete for source_document_id=%s; continuing ingest",
            job.source_document_id,
            exc_info=True,
        )


async def run_ingest_batch_job(payload: dict[str, Any]) -> None:
    """Run pipelines sequentially; optionally fuse after all complete."""
    jobs = [_ingest_job_from_dict(j) for j in payload["jobs"]]
    fuse_sources = bool(payload.get("fuse_sources", False))
    source_document_ids = [job.source_document_id for job in jobs]
    for job in jobs:
        await _wait_for_thumbnail_ready(job)
        await run_pipeline_for_source_job(job)
    if fuse_sources:
        await run_cross_source_fusion_job({"source_document_ids": [str(d) for d in source_document_ids]})
