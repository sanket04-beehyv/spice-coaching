"""Ingest start orchestration — queue pipeline for staged source documents.

Owns ``POST /admin/ingest``. Upload bytes via ``IngestUploadService`` first.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.attribution_audit import record_attribution_event
from platform_service.services.ingest_enqueue_service import (
    ingest_job_from_result,
)
from platform_service.services.ingest_errors import IngestValidationError
from platform_service.services.ingest_upload_service import (
    DuplicateIngestConflict,
    IngestedSourceResult,
    IngestUploadService,
)
from platform_service.services.run_state_service import ConcurrentRunError, RunStateService
from platform_service.workers.ingest_worker import IngestJob


@dataclass(frozen=True)
class IngestStartParams:
    assessment_mode: str
    uploaded_by: str
    ingestion_instructions: str | None = None
    target_cards_per_module: int | None = None
    target_quizzes_per_module: int | None = None


@dataclass(frozen=True)
class IngestStartSourcePayload:
    source_document_id: uuid.UUID
    run_id: uuid.UUID
    title: str
    source_type: str
    stored_path: str


@dataclass(frozen=True)
class IngestStartResult:
    batch_id: uuid.UUID
    sources: list[IngestStartSourcePayload]
    skipped_duplicates: list[DuplicateIngestConflict]
    jobs: list[IngestJob]


class IngestStartService:
    """Apply ingest metadata and enqueue Celery work for staged source documents."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._source_repo = SourceRepository(db)
        self._run_state = RunStateService(db)

    async def start(
        self,
        *,
        source_document_ids: list[uuid.UUID],
        params: IngestStartParams,
        override_flags: list[bool],
    ) -> IngestStartResult:
        if not source_document_ids:
            raise IngestValidationError("at least one source_document_id is required")
        if len(source_document_ids) > 10:
            raise IngestValidationError("at most 10 source_document_ids per request")

        docs_by_id = await self._load_documents(source_document_ids)
        skipped: list[DuplicateIngestConflict] = []
        targets: list[SourceDocument] = []

        for doc_id, override in zip(source_document_ids, override_flags, strict=True):
            doc = docs_by_id.get(doc_id)
            if doc is None:
                raise AppError(
                    ErrorCode.SOURCE_NOT_FOUND.value,
                    f"source_document {doc_id} not found",
                    status=404,
                )
            resolved = await self._resolve_target_document(doc, override_duplicate=override)
            if isinstance(resolved, DuplicateIngestConflict):
                skipped.append(resolved)
                continue
            targets.append(resolved)

        if not targets:
            raise AppError(
                ErrorCode.DUPLICATE_CONTENT.value,
                "One or more source documents match already-ingested content; set override to re-ingest.",
                status=409,
                extensions={
                    "conflicts": [IngestUploadService.duplicate_conflict_payload(c) for c in skipped],
                },
            )

        for doc in targets:
            await self._source_repo.update_status(doc.id, status="ingesting")

        batch = await self._run_state.create_batch(
            assessment_mode=params.assessment_mode,
            ingestion_instructions=params.ingestion_instructions,
            cards_per_module=params.target_cards_per_module,
            quizzes_per_module=params.target_quizzes_per_module,
        )
        source_payloads: list[IngestStartSourcePayload] = []
        jobs = []
        try:
            for doc in targets:
                run = await self._run_state.create_queued_run(
                    source_document_id=doc.id,
                    ingest_batch_id=batch.id,
                )
                result = IngestedSourceResult(
                    source_document_id=doc.id,
                    title=doc.title,
                    source_type=doc.source_type,
                    stored_path=doc.original_storage_path,
                    content_domain=doc.content_domain,
                )
                jobs.append(
                    ingest_job_from_result(
                        result,
                        run_id=run.id,
                        batch_id=batch.id,
                    )
                )
                source_payloads.append(
                    IngestStartSourcePayload(
                        source_document_id=doc.id,
                        run_id=run.id,
                        title=doc.title,
                        source_type=doc.source_type,
                        stored_path=doc.original_storage_path,
                    )
                )
                await self._record_ingest_started(doc, params)
        except ConcurrentRunError as exc:
            raise AppError(
                ErrorCode.CONCURRENT_RUN.value,
                (
                    f"source_document {exc.source_document_id} already has an active ingestion run "
                    f"({exc.existing_run_id})"
                ),
                status=409,
            ) from exc

        return IngestStartResult(
            batch_id=batch.id,
            sources=source_payloads,
            skipped_duplicates=skipped,
            jobs=jobs,
        )

    async def _load_documents(self, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, SourceDocument]:
        rows = await self._source_repo.list_source_documents_by_ids(document_ids)
        return {row.id: row for row in rows}

    async def _resolve_target_document(
        self,
        doc: SourceDocument,
        *,
        override_duplicate: bool,
    ) -> SourceDocument | DuplicateIngestConflict:
        if doc.status == "uploaded":
            return doc

        if doc.status == "ingested":
            if not override_duplicate:
                return DuplicateIngestConflict(
                    filename=doc.original_filename or doc.title,
                    title=doc.title,
                    content_sha256=doc.content_sha256 or "",
                    existing_source_documents=(doc,),
                )
            return await self._source_repo.clone_for_reingest(doc, uploaded_by=doc.uploaded_by)

        if doc.status == "failed":
            await self._source_repo.update_status(doc.id, status="ingesting")
            return doc

        if doc.status == "ingesting":
            active = await self._run_state.find_active_run(doc.id)
            if active is not None:
                raise AppError(
                    ErrorCode.CONCURRENT_RUN.value,
                    (f"source_document {doc.id} already has an active ingestion run ({active.id})"),
                    status=409,
                )
            return doc

        raise AppError(
            ErrorCode.SOURCE_NOT_UPLOADED.value,
            f"source_document {doc.id} has status {doc.status!r} and cannot be queued for ingest",
            status=422,
        )

    async def _record_ingest_started(self, doc: SourceDocument, params: IngestStartParams) -> None:
        audit_payload: dict[str, Any] = {
            "stored_path": doc.original_storage_path,
            "source_type": doc.source_type,
            "content_domain": doc.content_domain,
            "assessment_mode": params.assessment_mode,
        }
        if params.ingestion_instructions is not None:
            audit_payload["ingestion_instructions"] = params.ingestion_instructions
        if params.target_cards_per_module is not None:
            audit_payload["cards_per_module"] = params.target_cards_per_module
        if params.target_quizzes_per_module is not None:
            audit_payload["quizzes_per_module"] = params.target_quizzes_per_module
        await record_attribution_event(
            self._db,
            event_type="ingest_started",
            actor=params.uploaded_by,
            source_document_id=doc.id,
            payload=audit_payload,
        )
