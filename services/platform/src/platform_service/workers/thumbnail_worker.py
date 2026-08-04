"""Celery job: generate source_document thumbnail before ingest extraction."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from mc_contracts.errors import ErrorCode

from platform_service.db.base import SessionLocal
from platform_service.deps import get_object_storage_client
from platform_service.services.run_state_service import STAGE_THUMBNAIL, STEP_RUNNING, RunStateService
from platform_service.services.source_thumbnail_service import (
    SourceThumbnailService,
    source_type_supports_thumbnail,
)

logger = logging.getLogger(__name__)


async def run_thumbnail_job(payload: dict[str, Any]) -> None:
    """Generate thumbnail for one source_document. Never raises to the caller."""
    source_document_id = UUID(str(payload["source_document_id"]))
    source_path = str(payload["source_path"])
    source_type = str(payload["source_type"])
    run_raw = payload.get("run_id")
    run_id = UUID(str(run_raw)) if run_raw else None
    try:
        async with SessionLocal() as session:
            run_state = RunStateService(session)
            step_id: UUID | None = None
            if run_id is not None:
                if not source_type_supports_thumbnail(source_type):
                    await run_state.skip_step(
                        run_id=run_id,
                        stage=STAGE_THUMBNAIL,
                        reason="unsupported_source_type",
                        input_summary={"source_type": source_type},
                    )
                    await session.commit()
                    return
                step = await run_state.start_step(
                    run_id=run_id,
                    stage=STAGE_THUMBNAIL,
                    input_summary={"source_type": source_type},
                )
                step_id = step.id
                await session.commit()

            path = await SourceThumbnailService(
                session, storage=get_object_storage_client()
            ).generate_and_store(
                source_document_id=source_document_id,
                source_path=source_path,
                source_type=source_type,
            )

            if run_id is not None and step_id is not None:
                if path:
                    await run_state.complete_step(
                        step_id,
                        output_summary={"thumbnail_storage_path": path},
                    )
                else:
                    await run_state.fail_step(
                        step_id,
                        error_code=ErrorCode.THUMBNAIL_FAILED.value,
                        error_message="thumbnail generation returned no path",
                        error={
                            "type": "ThumbnailGenerationFailed",
                            "message": "thumbnail generation returned no path",
                        },
                    )
                await session.commit()
    except Exception:
        logger.exception(
            "Thumbnail job crashed source_document_id=%s source_type=%s",
            source_document_id,
            source_type,
        )
        if run_id is not None:
            try:
                async with SessionLocal() as session:
                    run_state = RunStateService(session)
                    existing = await run_state.find_step(run_id, stage=STAGE_THUMBNAIL)
                    if existing is not None and existing.status == STEP_RUNNING:
                        await run_state.fail_step(
                            existing.id,
                            error_code=ErrorCode.THUMBNAIL_FAILED.value,
                            error_message="thumbnail worker crashed",
                            error={
                                "type": "ThumbnailJobCrashed",
                                "message": "thumbnail worker crashed",
                            },
                        )
                        await session.commit()
            except Exception:
                logger.exception(
                    "Failed to record thumbnail step failure for run_id=%s",
                    run_id,
                )
