"""SSE pipeline streaming and post-publish poll tail for admin ingest."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.base import SessionLocal
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.object_storage import ObjectStorageClient
from platform_service.services.run_state_service import (
    POST_PUBLISH_STAGES,
    RUN_RUNNING,
    STEP_FAILED,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    RunStateService,
)
from platform_service.services.source_thumbnail_service import presign_thumbnail
from platform_service.workers.pipeline_orchestrator import PipelineOrchestrator

logger = logging.getLogger(__name__)

_POST_PUBLISH_SSE_POLL_INTERVAL_S = 1.0
_POST_PUBLISH_SSE_MAX_WAIT_S = 2 * 60 * 60


class IngestStreamService:
    """Owns SSE event generation and poll enrichment for ingest endpoints."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    async def thumbnail_poll_fields(
        session: AsyncSession,
        source_document_id: uuid.UUID,
        storage: ObjectStorageClient,
    ) -> dict[str, Any]:
        doc = await SourceRepository(session).get_source_document(source_document_id)
        if doc is None:
            return {"thumbnail_storage_path": None, "thumbnail_presigned_url": None}
        thumb_presign = await presign_thumbnail(storage, thumbnail_storage_path=doc.thumbnail_storage_path)
        return {
            "thumbnail_storage_path": doc.thumbnail_storage_path,
            "thumbnail_presigned_url": thumb_presign[0] if thumb_presign else None,
            "thumbnail_presigned_expires_seconds": thumb_presign[1] if thumb_presign else None,
        }

    @staticmethod
    async def post_publish_sse_tail_events(
        run_id: uuid.UUID,
        *,
        poll_interval_s: float = _POST_PUBLISH_SSE_POLL_INTERVAL_S,
        max_wait_s: float = _POST_PUBLISH_SSE_MAX_WAIT_S,
    ) -> AsyncIterator[dict[str, Any]]:
        """Poll ingestion_run_step rows until post-publish work finishes."""
        run_id_str = str(run_id)
        seen_started: set[str] = set()
        seen_terminal: set[str] = set()
        deadline = time.monotonic() + max_wait_s

        while time.monotonic() < deadline:
            async with SessionLocal() as session:
                state = RunStateService(session)
                run = await state.get_run(run_id)
                if run is None:
                    return
                post_publish = [s for s in await state.list_steps(run_id) if s.stage in POST_PUBLISH_STAGES]
                for step in post_publish:
                    sid = str(step.id)
                    if step.status == "running" and sid not in seen_started:
                        seen_started.add(sid)
                        yield {
                            "event": "stage_started",
                            "run_id": run_id_str,
                            "stage": step.stage,
                            "input_summary": step.input_summary_jsonb,
                        }
                    if (
                        step.status in (STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED)
                        and sid not in seen_terminal
                    ):
                        seen_terminal.add(sid)
                        if step.status == STEP_SUCCEEDED:
                            yield {
                                "event": "stage_succeeded",
                                "run_id": run_id_str,
                                "stage": step.stage,
                                "summary": step.output_summary_jsonb,
                            }
                        elif step.status == STEP_FAILED:
                            yield {
                                "event": "stage_failed",
                                "run_id": run_id_str,
                                "stage": step.stage,
                                "error": step.error_jsonb,
                            }
                        else:
                            yield {
                                "event": "stage_skipped",
                                "run_id": run_id_str,
                                "stage": step.stage,
                                "reason": (step.output_summary_jsonb or {}).get("skipped_reason"),
                            }
                if run.status != RUN_RUNNING:
                    yield {
                        "event": "pipeline_complete",
                        "run_id": run_id_str,
                        "final_status": run.status,
                    }
                    return
            await asyncio.sleep(poll_interval_s)

    @staticmethod
    async def pipeline_sse_lines(
        *,
        source_document_id: uuid.UUID,
        source_path: str,
        source_type: str,
        primary_language: str,
        skip_merge: bool,
    ) -> AsyncIterator[str]:
        """Yield SSE-formatted lines for one in-process pipeline run."""
        run_id: uuid.UUID | None = None
        pipeline_complete_emitted = False
        try:
            # Mirror PipelineOrchestrator.run_staged: release the driver session
            # before long-running staged work so we do not hold a pool slot for hours.
            async with SessionLocal() as stream_session:
                orch = PipelineOrchestrator(stream_session)
            async for event in orch.run_generator(
                source_document_id=source_document_id,
                source_path=source_path,
                source_type=source_type,
                primary_language=primary_language,
                skip_merge=skip_merge,
                staged_sessions=True,
            ):
                if event.get("event") == "run_started":
                    run_id = uuid.UUID(event["run_id"])
                if event.get("event") == "pipeline_complete":
                    pipeline_complete_emitted = True
                yield f"data: {json.dumps(event)}\n\n"
            if run_id is not None and not pipeline_complete_emitted:
                async for event in IngestStreamService.post_publish_sse_tail_events(run_id):
                    yield f"data: {json.dumps(event)}\n\n"
        except Exception:
            logger.exception("Streaming pipeline crashed for source_document_id=%s", source_document_id)
            yield f"data: {json.dumps({'event': 'error', 'detail': 'pipeline crashed'})}\n\n"
