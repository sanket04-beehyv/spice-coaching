"""Post-publish helpers (quiz gating by source assessment_mode)."""

from __future__ import annotations

from uuid import UUID

from mc_contracts.enums import AssessmentMode
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.source_repository import SourceRepository


async def should_generate_quiz_for_sources(
    session: AsyncSession,
    source_document_ids: list[UUID],
) -> bool:
    """Return True when at least one constituent source requests quizzes.

    When every linked source_document has assessment_mode=read_only, skip
    quiz generation. Empty id list defaults to True (legacy / unknown provenance).
    """
    if not source_document_ids:
        return True
    docs = await SourceRepository(session).list_source_documents_by_ids(source_document_ids)
    if not docs:
        return True
    return any(d.assessment_mode == AssessmentMode.WITH_QUIZ.value for d in docs)
