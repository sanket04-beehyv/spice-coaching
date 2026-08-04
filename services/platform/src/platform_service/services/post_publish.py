"""Post-publish helpers (quiz gating by ingest_batch assessment_mode)."""

from __future__ import annotations

from uuid import UUID

from mc_contracts.enums import AssessmentMode
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.services.ingestion_cardinality import load_batch_for_run


async def should_generate_quiz_for_run(
    session: AsyncSession,
    ingestion_run_id: UUID,
) -> bool:
    """Return True when the run's batch requests quizzes.

    ``assessment_mode=read_only`` skips quiz generation. Missing batch defaults
    to True (legacy / unknown provenance).
    """
    batch = await load_batch_for_run(session, ingestion_run_id)
    if batch is None:
        return True
    return batch.assessment_mode == AssessmentMode.WITH_QUIZ.value
