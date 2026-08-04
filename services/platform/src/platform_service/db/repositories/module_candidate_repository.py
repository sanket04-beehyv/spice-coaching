"""module_candidate_draft repository.

Per `docs/ARCHITECTURE_RESET.md`. Candidates are ephemeral pipeline state
for Stage 2 retry semantics and run-history visibility — not a reviewer
queue. The W-6 review-status / claim / reviewer columns were dropped in
migration 0007; this repo no longer carries an `update_review_status`
method.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mc_contracts.localized import LocalizedString
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft


class ModuleCandidateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_candidate(
        self,
        *,
        ingestion_run_id: UUID,
        proposed_title: str,
        scope_summary: str,
        description_localized: LocalizedString | None = None,
        domain: str | None = None,
        source_provenance: list[dict[str, Any]],
        estimated_card_count: int,
        estimated_quiz_count: int,
        proposed_module_type: str,
        behavioural_gap_code: str | None = None,
        quality_flags: dict[str, Any] | None = None,
        clinical_review_notes: str | None = None,
        previous_practice_summary: str | None = None,
        current_practice_summary: str | None = None,
        rationale_summary: str | None = None,
        ingestion_instruction_rationale: str | None = None,
        source_chunk_ids: list[str] | None = None,
    ) -> ModuleCandidateDraft:
        cand = ModuleCandidateDraft(
            ingestion_run_id=ingestion_run_id,
            proposed_title=proposed_title,
            behavioural_gap_code=behavioural_gap_code,
            scope_summary=scope_summary,
            description_localized=description_localized,
            domain=domain,
            source_provenance_jsonb=source_provenance,
            estimated_card_count=estimated_card_count,
            estimated_quiz_count=estimated_quiz_count,
            proposed_module_type=proposed_module_type,
            quality_flags_jsonb=quality_flags,
            clinical_review_notes=clinical_review_notes,
            previous_practice_summary=previous_practice_summary,
            current_practice_summary=current_practice_summary,
            rationale_summary=rationale_summary,
            ingestion_instruction_rationale=ingestion_instruction_rationale,
            source_chunk_ids=source_chunk_ids,
        )
        self._session.add(cand)
        await self._session.flush()
        return cand

    async def get_candidate(self, candidate_id: UUID) -> ModuleCandidateDraft | None:
        return await self._session.get(ModuleCandidateDraft, candidate_id)

    async def list_candidates_for_run(self, ingestion_run_id: UUID) -> list[ModuleCandidateDraft]:
        result = await self._session.execute(
            select(ModuleCandidateDraft)
            .where(ModuleCandidateDraft.ingestion_run_id == ingestion_run_id)
            .order_by(ModuleCandidateDraft.created_at)
        )
        return list(result.scalars().all())

    async def list_candidates_for_chunk(
        self,
        ingestion_run_id: UUID,
        *,
        chunk_id: str,
    ) -> list[ModuleCandidateDraft]:
        """Return drafts whose source_chunk_ids contains ``chunk_id``."""
        candidates = await self.list_candidates_for_run(ingestion_run_id)
        return [
            c for c in candidates if isinstance(c.source_chunk_ids, list) and chunk_id in c.source_chunk_ids
        ]

    async def delete_candidates_for_run(self, ingestion_run_id: UUID) -> int:
        result = await self._session.execute(
            delete(ModuleCandidateDraft).where(ModuleCandidateDraft.ingestion_run_id == ingestion_run_id)
        )
        await self._session.flush()
        return int(result.rowcount or 0)

    async def delete_candidates_for_chunk(
        self,
        ingestion_run_id: UUID,
        *,
        chunk_id: str,
        exclude_ids: set[UUID] | None = None,
    ) -> list[UUID]:
        """Delete drafts attributed to ``chunk_id``; return deleted ids.

        ``exclude_ids`` skips candidates that have already progressed
        (e.g. card_draft started) so sibling draft branches stay intact.
        """
        skip = exclude_ids or set()
        to_delete = [
            c
            for c in await self.list_candidates_for_chunk(ingestion_run_id, chunk_id=chunk_id)
            if c.id not in skip
        ]
        deleted_ids = [c.id for c in to_delete]
        for cand in to_delete:
            await self._session.delete(cand)
        if deleted_ids:
            await self._session.flush()
        return deleted_ids

    async def delete_candidate(self, candidate_id: UUID) -> bool:
        cand = await self.get_candidate(candidate_id)
        if cand is None:
            return False
        await self._session.delete(cand)
        await self._session.flush()
        return True

    async def update_quality_flags(
        self,
        candidate_id: UUID,
        quality_flags: dict[str, Any] | None,
    ) -> ModuleCandidateDraft | None:
        cand = await self.get_candidate(candidate_id)
        if cand is None:
            return None
        cand.quality_flags_jsonb = quality_flags
        await self._session.flush()
        return cand
