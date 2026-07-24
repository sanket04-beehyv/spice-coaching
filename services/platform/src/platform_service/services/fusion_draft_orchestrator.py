"""Draft orchestration for fused module candidates (Stage 3 + coverage)."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.services.draft_pipeline import DraftPipeline
from platform_service.workers.stage_d_draft import StageDOrchestrator

logger = logging.getLogger(__name__)


class FusionDraftOrchestrator:
    """Draft fused candidates via Stage D with cross-source coverage checks."""

    def __init__(
        self,
        session: AsyncSession,
        stage_d: StageDOrchestrator,
        *,
        draft_pipeline: DraftPipeline | None = None,
        fusion_run_id: UUID | None = None,
    ) -> None:
        self._session = session
        self._stage_d = stage_d
        self._pipeline = draft_pipeline or getattr(stage_d, "_pipeline", None)
        self._fusion_run_id = fusion_run_id

    def bind_fusion_run(self, fusion_run_id: UUID) -> None:
        self._fusion_run_id = fusion_run_id

    async def draft_with_coverage(
        self,
        candidate_id: UUID,
        expected_sources: set[str],
        *,
        step_id: UUID | None = None,
    ) -> tuple[UUID | None, int, str | None, bool]:
        """Draft + verify cross-source coverage. Up to 2 attempts.

        After each successful draft, query the new module's card-level
        block citations and check that every `expected_source` appears.
        On failure: delete the just-drafted module and retry once. If
        the second attempt also fails coverage, accept the result and
        return coverage_ok=False so the caller can flag for reviewer.
        """
        last_module_id: UUID | None = None
        last_cards = 0
        last_reason: str | None = None
        for attempt in (1, 2):
            module_id, cards, reason = await self._draft_with_retry(candidate_id, step_id=step_id)
            if module_id is None:
                return None, 0, reason, False
            last_module_id, last_cards, last_reason = module_id, cards, reason
            actual_sources = await self._cards_source_set(module_id)
            missing = expected_sources - actual_sources
            if not missing:
                await self._enqueue_post_publish(module_id, candidate_id=candidate_id)
                return module_id, cards, reason, True
            logger.warning(
                "Stage 2b: fused module %s attempt %d cards span %d/%d expected sources; %s",
                module_id,
                attempt,
                len(actual_sources & expected_sources),
                len(expected_sources),
                "retrying" if attempt < 2 else "accepting (best-effort), flagged for reviewer",
            )
            if attempt < 2:
                await self._session.execute(
                    text("DELETE FROM module WHERE id=:mid"),
                    {"mid": str(module_id)},
                )
                await self._session.commit()
                last_module_id = None
        if last_module_id is not None:
            await self._enqueue_post_publish(last_module_id, candidate_id=candidate_id)
        return last_module_id, last_cards, last_reason, False

    async def _cards_source_set(self, module_id: UUID) -> set[str]:
        """Return source_document_ids cited by the module's cards."""
        rows = await self._session.execute(
            text("""
                SELECT DISTINCT sp.source_document_id::text
                FROM module_card mc
                CROSS JOIN LATERAL unnest(mc.source_block_ids) AS bid
                JOIN content_block bk ON bk.id = bid
                JOIN source_page sp ON sp.id = bk.source_page_id
                WHERE mc.module_id = :mid
            """),
            {"mid": str(module_id)},
        )
        return {row[0] for row in rows.all()}

    async def _draft_with_retry(
        self,
        candidate_id: UUID,
        *,
        step_id: UUID | None = None,
    ) -> tuple[UUID | None, int, str | None]:
        """Run StageDOrchestrator with one retry on transient errors."""
        last_exc: Exception | None = None
        for attempt in (1, 2):
            try:
                d_result = await self._stage_d.run(
                    candidate_id=candidate_id,
                    enqueue_post_publish=False,
                    step_id=step_id,
                )
                await self._session.commit()
                return d_result.module_id, d_result.cards_count, d_result.insufficient_reason
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Stage 3 drafter attempt %d failed for fused candidate %s: %s",
                    attempt,
                    candidate_id,
                    type(exc).__name__,
                )
                await self._session.rollback()
        return None, 0, type(last_exc).__name__ if last_exc else "Unknown"

    async def _enqueue_post_publish(self, module_id: UUID, *, candidate_id: UUID) -> None:
        """Fire post-publish Celery tasks for a module kept after coverage."""
        module = await self._session.get(Module, module_id)
        source_ids = list(module.source_document_ids or []) if module else []
        if self._pipeline is not None:
            await self._pipeline.enqueue_post_publish(
                module_id,
                source_ids,
                ingestion_run_id=self._fusion_run_id,
                candidate_id=candidate_id,
            )
            return
        await self._stage_d._enqueue_post_publish(
            module_id,
            source_ids,
            ingestion_run_id=self._fusion_run_id,
            candidate_id=candidate_id,
        )
