"""Load module candidates for cross-source fusion from ingestion runs."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def load_fusion_candidates(
    session: AsyncSession,
    doc_ids: list[UUID],
) -> list[dict[str, Any]]:
    """For each source document, load candidates from its latest successful run."""
    rows = (
        (
            await session.execute(
                text("""
                    WITH latest_run AS (
                        SELECT DISTINCT ON (source_document_id)
                               id, source_document_id
                        FROM ingestion_run
                        WHERE source_document_id = ANY(:doc_ids)
                          AND status IN ('succeeded', 'partially_succeeded')
                          AND COALESCE(error_jsonb->>'type', '') != 'cross_source_fusion'
                        ORDER BY source_document_id, started_at DESC
                    )
                    SELECT d.id::text                       AS id,
                           lr.source_document_id::text      AS source_document_id,
                           d.proposed_title                 AS proposed_title,
                           d.scope_summary                  AS scope_summary,
                           d.proposed_module_type           AS proposed_module_type,
                           d.estimated_card_count           AS estimated_card_count,
                           d.estimated_quiz_count           AS estimated_quiz_count,
                           d.source_provenance_jsonb        AS source_provenance,
                           d.quality_flags_jsonb            AS quality_flags
                    FROM module_candidate_draft d
                    JOIN latest_run lr ON lr.id = d.ingestion_run_id
                    ORDER BY lr.source_document_id, d.created_at
                """),
                {"doc_ids": [str(d) for d in doc_ids]},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
