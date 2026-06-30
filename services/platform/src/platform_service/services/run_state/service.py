"""Pipeline run state service — run lifecycle orchestration."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.services.run_state.claims import RunClaimMixin
from platform_service.services.run_state.constants import (
    FUSION_RUN_TYPE,
    RUN_PARTIALLY_SUCCEEDED,
    RUN_RUNNING,
    ConcurrentFusionRunError,
    ConcurrentRunError,
)
from platform_service.services.run_state.steps import RunStepMixin


class RunStateService(RunClaimMixin, RunStepMixin):
    """Persists run + step state for the pipeline orchestrator."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_active_run(self, source_document_id: UUID) -> IngestionRun | None:
        result = await self._session.execute(
            select(IngestionRun)
            .where(
                IngestionRun.source_document_id == source_document_id,
                IngestionRun.status == RUN_RUNNING,
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def find_active_fusion_run_for_document(self, source_document_id: UUID) -> IngestionRun | None:
        doc_str = str(source_document_id)
        result = await self._session.execute(
            select(IngestionRun)
            .where(
                IngestionRun.status == RUN_RUNNING,
                IngestionRun.error_jsonb["type"].astext == FUSION_RUN_TYPE,
                IngestionRun.error_jsonb["source_document_ids"].contains([doc_str]),
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _lock_source_document(self, source_document_id: UUID) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:doc_id))"),
            {"doc_id": str(source_document_id)},
        )

    async def assert_no_active_fusion_overlap(self, source_document_ids: list[UUID]) -> None:
        for doc_id in source_document_ids:
            active_ingest = await self.find_active_run(doc_id)
            if active_ingest is not None:
                raise ConcurrentRunError(doc_id, active_ingest.id)
            active_fusion = await self.find_active_fusion_run_for_document(doc_id)
            if active_fusion is not None:
                raise ConcurrentFusionRunError(doc_id, active_fusion.id)

    async def start_run(
        self,
        *,
        source_document_id: UUID,
        triggered_by: UUID | None = None,
    ) -> IngestionRun:
        await self._lock_source_document(source_document_id)
        existing = await self.find_active_run(source_document_id)
        if existing is not None:
            raise ConcurrentRunError(source_document_id, existing.id)
        run = IngestionRun(
            source_document_id=source_document_id,
            status=RUN_RUNNING,
            triggered_by=triggered_by,
        )
        self._session.add(run)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            existing = await self.find_active_run(source_document_id)
            if existing is not None:
                raise ConcurrentRunError(source_document_id, existing.id) from exc
            raise
        return run

    async def start_fusion_run(
        self,
        *,
        source_document_ids: list[UUID],
    ) -> IngestionRun:
        if not source_document_ids:
            raise ValueError("source_document_ids must not be empty")
        anchor = source_document_ids[0]
        await self._lock_source_document(anchor)
        await self.assert_no_active_fusion_overlap(source_document_ids)
        run = IngestionRun(
            source_document_id=anchor,
            status=RUN_RUNNING,
            error_jsonb={
                "type": FUSION_RUN_TYPE,
                "source_document_ids": [str(d) for d in source_document_ids],
            },
        )
        self._session.add(run)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            existing = await self.find_active_run(anchor)
            if existing is not None:
                raise ConcurrentRunError(anchor, existing.id) from exc
            raise
        return run

    async def find_resumable_run(self, source_document_id: UUID) -> IngestionRun | None:
        result = await self._session.execute(
            select(IngestionRun)
            .where(
                IngestionRun.source_document_id == source_document_id,
                IngestionRun.status.in_((RUN_RUNNING, RUN_PARTIALLY_SUCCEEDED)),
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def is_fusion_run(run: IngestionRun) -> bool:
        return (run.error_jsonb or {}).get("type") == FUSION_RUN_TYPE

    async def find_best_poll_run(self, source_document_id: UUID) -> IngestionRun | None:
        run = await self.find_active_run(source_document_id)
        if run is not None and self.is_fusion_run(run):
            run = None
        if run is None:
            run = await self.find_resumable_run(source_document_id)
            if run is not None and self.is_fusion_run(run):
                run = None
        if run is not None:
            return run

        result = await self._session.execute(
            select(IngestionRun)
            .where(IngestionRun.source_document_id == source_document_id)
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        run = result.scalar_one_or_none()
        if run is None or not self.is_fusion_run(run):
            return run

        result = await self._session.execute(
            select(IngestionRun)
            .where(
                IngestionRun.source_document_id == source_document_id,
                IngestionRun.error_jsonb["type"].astext != FUSION_RUN_TYPE,
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
