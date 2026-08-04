"""Admin v3.3 ingest endpoint — drives a real source document through the
PipelineOrchestrator (Stage A → B → C → D) and exposes batch status polling.

Distinct from the legacy `POST /admin/documents/upload` (scenarios pipeline
on the Document/Scenario tables); this endpoint operates on the v3.3
`source_document` + `ingestion_run` + `module_candidate_draft` tables.

Endpoints:
  POST /admin/ingest/upload        — upload one or more files (stage source_document rows)
  POST /admin/ingest               — queue ingest for staged source_document_ids
  GET  /admin/ingest/batches/{batch_id} — poll batch tree progress across sources (+ fusion)
  POST /admin/ingest/batches/{batch_id}/retry — retry every retryable failed stage in the batch
  POST /admin/ingest/modules/{module_id}/override-merge — promote secondary dual-path merge module

Uploaded bytes are stored in object storage under the ``ingest/`` prefix. The API
returns ``stored_path`` as ``{bucket}/{object_key}``. The pipeline downloads
the object to a temp file for Stage A extractors, then deletes the temp file.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from mc_contracts.admin_ingest import (
    ExistingIngestedSourceSummary,
    IngestBatchRetryResponse,
    IngestBatchRetryStageResult,
    IngestDuplicateConflict,
    IngestMergeOverrideResponse,
    IngestStartAcceptedSource,
    IngestStartRequest,
    IngestStartResponse,
    IngestUploadedSource,
    IngestUploadResponse,
)
from mc_contracts.errors import ErrorCode
from mc_foundation.objectstore import ObjectStore
from mc_foundation.problem import AppError
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import resolve_tenant_id_for_admin
from platform_service.auth.spice_user import resolve_spice_actor
from platform_service.config import get_settings
from platform_service.deps import get_db, get_object_storage_client
from platform_service.services.ingest_batch_poll_presenter import IngestBatchPollPresenter
from platform_service.services.ingest_enqueue_service import enqueue_thumbnail_and_batch
from platform_service.services.ingest_merge_override_service import IngestMergeOverrideService
from platform_service.services.ingest_retry_service import IngestRetryService
from platform_service.services.ingest_start_service import IngestStartParams, IngestStartService
from platform_service.services.ingest_upload_service import (
    DuplicateIngestConflict,
    IngestUploadParams,
    IngestUploadService,
)
from platform_service.services.ingestion_instruction_sanitizer import (
    sanitize_ingestion_instructions,
)

router = APIRouter(
    prefix="/admin",
    tags=["admin-ingest"],
)

_DUPLICATE_CONTENT_MESSAGE = (
    "One or more files match already-uploaded or already-ingested content; set override to re-upload."
)


def _duplicate_content_error(conflicts: list[DuplicateIngestConflict]) -> AppError:
    return AppError(
        ErrorCode.DUPLICATE_CONTENT.value,
        _DUPLICATE_CONTENT_MESSAGE,
        status=409,
        extensions={"conflicts": [IngestUploadService.duplicate_conflict_payload(c) for c in conflicts]},
    )


def _conflict_to_contract(conflict: DuplicateIngestConflict) -> IngestDuplicateConflict:
    return IngestDuplicateConflict(
        filename=conflict.filename,
        title=conflict.title,
        content_sha256=conflict.content_sha256,
        existing_source_documents=[
            ExistingIngestedSourceSummary(
                source_document_id=doc.id,
                title=doc.title,
                original_filename=doc.original_filename,
                ingested_at=doc.ingested_at,
                status=doc.status,
            )
            for doc in conflict.existing_source_documents
        ],
    )


def _resolve_ingestion_instructions(raw: str | None) -> str | None:
    settings = get_settings()
    result = sanitize_ingestion_instructions(
        raw,
        max_length=settings.ingestion_instructions_max_length,
    )
    if result.rejected:
        raise AppError(
            ErrorCode.INVALID_INGESTION_INSTRUCTIONS.value,
            result.rejection_reason or "Invalid ingestion instructions",
            status=422,
        )
    return result.text


def _resolve_optional_positive_int(
    raw: int | str | None,
    *,
    field_name: str,
) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw.strip() == "":
            return None
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise AppError(
                ErrorCode.INVALID_CARDINALITY_TARGETS.value,
                f"{field_name} must be an integer",
                status=422,
            ) from exc
    else:
        value = raw
    if value <= 0:
        raise AppError(
            ErrorCode.INVALID_CARDINALITY_TARGETS.value,
            f"{field_name} must be a positive integer",
            status=422,
        )
    return value


@router.post("/ingest/upload", status_code=201, response_model=IngestUploadResponse)
async def upload_ingest_documents(
    request: Request,
    files: list[UploadFile] = File(
        ...,
        description="One or more source files (PDF/PPTX/DOCX/audio/video); max 10",
    ),
    titles: str | None = Form(
        None,
        description='Optional JSON array of titles, one per file in upload order (e.g. ["BRAC SOP","UHIS guide"])',
    ),
    descriptions: str | None = Form(
        None,
        description=("Optional JSON array of descriptions (string or null), one per file in upload order"),
    ),
    override_duplicates: str | None = Form(
        None,
        description=(
            "Optional JSON array of booleans, one per file in upload order. When true, "
            "re-upload even if the file's content_sha256 matches an already-uploaded "
            "or already-ingested source_document."
        ),
    ),
    content_domains: str | None = Form(
        None,
        description=(
            "Optional JSON array of content domains, one per file in upload order "
            '(e.g. ["clinical","digital"]). Null/empty entries default to "clinical". '
            "Allowed: clinical, digital, operational."
        ),
    ),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_object_storage_client),
) -> IngestUploadResponse:
    """Upload one or more source documents to object storage without queueing ingest."""
    _ = resolve_tenant_id_for_admin(request, tenant_id)
    upload_svc = IngestUploadService(db, storage)
    upload_svc.validate_file_count(files)

    resolved_titles = upload_svc.resolve_titles_for_files(titles, files)
    resolved_descriptions = upload_svc.resolve_descriptions_for_files(descriptions, files)
    override_flags = upload_svc.resolve_override_duplicates_for_files(override_duplicates, files)
    resolved_content_domains = upload_svc.resolve_content_domains_for_files(content_domains, files)
    uploaded_by = resolve_spice_actor(request)
    params = IngestUploadParams(uploaded_by=uploaded_by)
    outcomes = await upload_svc.upload_files(
        files=files,
        titles=resolved_titles,
        descriptions=resolved_descriptions,
        params=params,
        override_flags=override_flags,
        content_domains=resolved_content_domains,
    )
    uploaded = [outcome.uploaded for outcome in outcomes if outcome.uploaded is not None]
    skipped_duplicates = [outcome.skipped for outcome in outcomes if outcome.skipped is not None]

    if not uploaded:
        raise _duplicate_content_error(skipped_duplicates)

    await db.commit()

    return IngestUploadResponse(
        sources=[
            IngestUploadedSource(
                source_document_id=result.source_document_id,
                title=result.title,
                source_type=result.source_type,
                stored_path=result.stored_path,
                content_domain=result.content_domain,
                status="uploaded",
            )
            for result in uploaded
        ],
        skipped_duplicates=[_conflict_to_contract(conflict) for conflict in skipped_duplicates],
    )


@router.post("/ingest", status_code=202, response_model=IngestStartResponse)
async def start_ingest(
    request: Request,
    body: IngestStartRequest,
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
) -> IngestStartResponse:
    """Queue the ingestion pipeline for one or more staged source documents."""
    _ = resolve_tenant_id_for_admin(request, tenant_id)
    upload_svc = IngestUploadService(db)
    target_cards = _resolve_optional_positive_int(body.cards_per_module, field_name="cards_per_module")
    target_quizzes = _resolve_optional_positive_int(body.quizzes_per_module, field_name="quizzes_per_module")
    sanitized_instructions = _resolve_ingestion_instructions(body.ingestion_instructions)
    upload_svc.validate_cardinality_targets(
        target_cards_per_module=target_cards,
        target_quizzes_per_module=target_quizzes,
    )
    upload_svc.validate_assessment_mode(body.assessment_mode)
    override_flags = upload_svc.resolve_override_duplicates_for_ids(
        body.override_duplicates,
        body.source_document_ids,
    )
    uploaded_by = resolve_spice_actor(request)
    params = IngestStartParams(
        assessment_mode=body.assessment_mode,
        uploaded_by=uploaded_by,
        ingestion_instructions=sanitized_instructions,
        target_cards_per_module=target_cards,
        target_quizzes_per_module=target_quizzes,
    )
    result = await IngestStartService(db).start(
        source_document_ids=body.source_document_ids,
        params=params,
        override_flags=override_flags,
    )
    await db.commit()

    enqueue_thumbnail_and_batch(
        result.jobs,
        batch_id=result.batch_id,
    )

    return IngestStartResponse(
        batch_id=result.batch_id,
        poll_url=get_settings().api_path(f"/admin/ingest/batches/{result.batch_id}"),
        sources=[
            IngestStartAcceptedSource(
                source_document_id=source.source_document_id,
                run_id=source.run_id,
                title=source.title,
                source_type=source.source_type,
                stored_path=source.stored_path,
            )
            for source in result.sources
        ],
        skipped_duplicates=[_conflict_to_contract(conflict) for conflict in result.skipped_duplicates],
    )


@router.get("/ingest/batches/{batch_id}")
async def get_ingest_batch_status(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Poll tree-shaped progress for one ingest batch (all sources + optional fusion)."""
    presenter = IngestBatchPollPresenter(db)
    payload = await presenter.present_batch(batch_id)
    if payload is None:
        raise AppError(ErrorCode.BATCH_NOT_FOUND.value, "ingest batch not found", status=404)
    return payload


@router.post(
    "/ingest/batches/{batch_id}/retry",
    response_model=IngestBatchRetryResponse,
)
async def retry_ingest_batch(
    batch_id: uuid.UUID,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> IngestBatchRetryResponse:
    """Retry every retryable failed stage in a batch in one call."""
    result = await IngestRetryService(db).retry_batch(batch_id)

    if any(item.status == "retry_queued" for item in result.results):
        response.status_code = 202
    return IngestBatchRetryResponse(
        batch_id=result.batch_id,
        results=[
            IngestBatchRetryStageResult(
                run_id=item.run_id,
                stage=item.stage,
                status=item.status,
                candidate_id=item.candidate_id,
                chunk_id=item.chunk_id,
                reason=item.reason,
            )
            for item in result.results
        ],
        poll_url=result.poll_url,
    )


@router.post(
    "/ingest/modules/{module_id}/override-merge",
    response_model=IngestMergeOverrideResponse,
)
async def override_ingest_merge(
    module_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> IngestMergeOverrideResponse:
    """Promote the secondary dual-path merge module; retire primary and matched source."""
    result = await IngestMergeOverrideService(db).override(module_id)
    return IngestMergeOverrideResponse(
        primary_module_id=result.primary_module_id,
        secondary_module_id=result.secondary_module_id,
        source_module_id=result.source_module_id,
        secondary_lifecycle_status=result.secondary_lifecycle_status,
    )
