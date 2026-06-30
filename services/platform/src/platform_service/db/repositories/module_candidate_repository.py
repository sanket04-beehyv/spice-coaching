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
from sqlalchemy import select
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
    ) -> ModuleCandidateDraft:
        cand = ModuleCandidateDraft(
            ingestion_run_id=ingestion_run_id,
            proposed_title=proposed_title,
            behavioural_gap_code=behavioural_gap_code,
            scope_summary=scope_summary,
            description_localized=description_localized,
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
