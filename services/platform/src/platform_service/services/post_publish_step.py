"""Helpers for ingestion_run_step rows tied to post-publish Celery workers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from platform_service.db.base import SessionLocal
from platform_service.db.models.ingestion_run import IngestionRunStep
from platform_service.services.run_state_service import RunStateService


async def finish_post_publish_step(
    *,
    step_id: UUID | None,
    success: bool,
    output_summary: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    """Complete or fail a post-publish step and maybe finalize the ingestion run.

    When ``step_id`` is None (manual regenerate from admin), this is a no-op.
    """
    if step_id is None:
        return

    async with SessionLocal() as session:
        run_state = RunStateService(session)
        if success:
            await run_state.complete_step(step_id, output_summary=output_summary)
        else:
            await run_state.fail_step(
                step_id,
                error=error or {"type": "PostPublishError", "message": "post-publish worker failed"},
            )
        step = await session.get(IngestionRunStep, step_id)
        if step is not None:
            await run_state.maybe_finalize_ingestion_run(step.ingestion_run_id)
        await session.commit()
