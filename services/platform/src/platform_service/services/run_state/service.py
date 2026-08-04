"""Pipeline run state service — run lifecycle orchestration."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.ingest_batch import IngestBatch
from platform_service.db.models.ingestion_run import IngestionRun
from platform_service.services.run_state.claims import RunClaimMixin
from platform_service.services.run_state.constants import (
    _ACTIVE_RUN_STATUSES,
    BATCH_FAILED,
    BATCH_PARTIALLY_SUCCEEDED,
    BATCH_QUEUED,
    BATCH_SUCCEEDED,
    FUSION_RUN_TYPE,
    RUN_PARTIALLY_SUCCEEDED,
    RUN_QUEUED,
    RUN_RUNNING,
    ConcurrentFusionRunError,
    ConcurrentRunError,
    as_error_object,
    now_utc,
    rollup_batch_status,
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
                IngestionRun.status.in_(tuple(_ACTIVE_RUN_STATUSES)),
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

    async def create_batch(
        self,
        *,
        assessment_mode: str = "with_quiz",
        ingestion_instructions: str | None = None,
        cards_per_module: int | None = None,
        quizzes_per_module: int | None = None,
        triggered_by: UUID | None = None,
    ) -> IngestBatch:
        batch = IngestBatch(
            status=BATCH_QUEUED,
            assessment_mode=assessment_mode,
            ingestion_instructions=ingestion_instructions,
            cards_per_module=cards_per_module,
            quizzes_per_module=quizzes_per_module,
            triggered_by=triggered_by,
        )
        self._session.add(batch)
        await self._session.flush()
        return batch

    async def get_batch(self, batch_id: UUID) -> IngestBatch | None:
        return await self._session.get(IngestBatch, batch_id)

    async def list_runs_for_batch(self, batch_id: UUID) -> list[IngestionRun]:
        result = await self._session.execute(
            select(IngestionRun)
            .where(IngestionRun.ingest_batch_id == batch_id)
            .order_by(IngestionRun.started_at.asc(), IngestionRun.id.asc())
        )
        return list(result.scalars().all())

    async def create_queued_run(
        self,
        *,
        source_document_id: UUID,
        ingest_batch_id: UUID,
        triggered_by: UUID | None = None,
    ) -> IngestionRun:
        await self._lock_source_document(source_document_id)
        existing = await self.find_active_run(source_document_id)
        if existing is not None:
            raise ConcurrentRunError(source_document_id, existing.id)
        run = IngestionRun(
            source_document_id=source_document_id,
            ingest_batch_id=ingest_batch_id,
            status=RUN_QUEUED,
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

    async def activate_queued_run(self, run_id: UUID) -> IngestionRun:
        """Attach to a queued/running/partially_succeeded run for pipeline work.

        Retry reopens terminal runs to ``running`` before enqueue; this also
        accepts ``partially_succeeded`` so a worker can attach safely.
        """
        run = await self.get_run(run_id)
        if run is None:
            raise ValueError(f"ingestion_run {run_id} not found")
        if run.status == RUN_RUNNING:
            return run
        if run.status == RUN_QUEUED:
            run.status = RUN_RUNNING
            await self._session.flush()
            return run
        if run.status == RUN_PARTIALLY_SUCCEEDED:
            run.status = RUN_RUNNING
            run.completed_at = None
            await self._session.flush()
            return run
        raise ValueError(f"ingestion_run {run_id} cannot be activated from status {run.status!r}")

    async def refresh_batch_status(self, batch_id: UUID) -> IngestBatch | None:
        batch = await self.get_batch(batch_id)
        if batch is None:
            return None
        runs = await self.list_runs_for_batch(batch_id)
        status = rollup_batch_status([r.status for r in runs])
        batch.status = status
        if status in (
            BATCH_SUCCEEDED,
            BATCH_FAILED,
            BATCH_PARTIALLY_SUCCEEDED,
        ):
            if batch.completed_at is None:
                batch.completed_at = now_utc()
        else:
            batch.completed_at = None
        await self._session.flush()
        return batch

    async def batch_has_awaiting_input(self, batch_id: UUID) -> bool:
        """True when any non-fusion run in the batch has an awaiting_input step."""
        runs = await self.list_runs_for_batch(batch_id)
        for run in runs:
            if self.is_fusion_run(run):
                continue
            if await self.run_has_awaiting_input(run.id):
                return True
        return False

    async def start_run(
        self,
        *,
        source_document_id: UUID,
        triggered_by: UUID | None = None,
        ingest_batch_id: UUID | None = None,
    ) -> IngestionRun:
        await self._lock_source_document(source_document_id)
        existing = await self.find_active_run(source_document_id)
        if existing is not None:
            raise ConcurrentRunError(source_document_id, existing.id)
        run = IngestionRun(
            source_document_id=source_document_id,
            ingest_batch_id=ingest_batch_id,
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
        ingest_batch_id: UUID | None = None,
    ) -> IngestionRun:
        if not source_document_ids:
            raise ValueError("source_document_ids must not be empty")
        anchor = source_document_ids[0]
        await self._lock_source_document(anchor)
        await self.assert_no_active_fusion_overlap(source_document_ids)
        run = IngestionRun(
            source_document_id=anchor,
            ingest_batch_id=ingest_batch_id,
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
        return as_error_object(run.error_jsonb).get("type") == FUSION_RUN_TYPE

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
