"""Admin v3.3 ingest endpoint — drives a real source document through the
PipelineOrchestrator (Stage A → B → C → D) and exposes status polling.

Distinct from the legacy `POST /admin/documents/upload` (scenarios pipeline
on the Document/Scenario tables); this endpoint operates on the v3.3
`source_document` + `ingestion_run` + `module_candidate_draft` tables.

Endpoints:
  POST /admin/ingest              — upload one or more files + start pipeline (optional cross-source fusion)
  GET  /admin/ingest/by-document/{source_document_id} — poll run + step state + emitted candidates

Uploaded bytes are stored in MinIO under the ``ingest/`` prefix. The API
returns ``stored_path`` as ``{bucket}/{object_key}``. The pipeline downloads
the object to a temp file for Stage A extractors, then deletes the temp file.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import resolve_tenant_id_for_admin
from platform_service.auth.spice_user import resolve_spice_actor
from platform_service.config import get_settings
from platform_service.deps import get_db, get_object_storage_client
from platform_service.services.ingest_enqueue_service import enqueue_thumbnail_and_batch
from platform_service.services.ingest_errors import IngestValidationError
from platform_service.services.ingest_upload_service import (
    DuplicateIngestConflict,
    IngestUploadParams,
    IngestUploadService,
)
from platform_service.services.ingestion_instruction_sanitizer import (
    sanitize_ingestion_instructions,
)
from platform_service.services.ingestion_run_presenter import IngestionRunPresenter
from platform_service.services.object_storage import ObjectStorageClient
from platform_service.services.run_state_service import RunStateService
from platform_service.services.source_thumbnail_service import thumbnail_poll_fields

router = APIRouter(
    prefix="/admin",
    tags=["admin-ingest"],
)

_DUPLICATE_CONTENT_CODE = "duplicate_content"
_DUPLICATE_CONTENT_MESSAGE = "One or more files match already-ingested content; set override to re-ingest."
_INVALID_INGESTION_INSTRUCTIONS_CODE = "invalid_ingestion_instructions"
_INVALID_CARDINALITY_TARGETS_CODE = "invalid_cardinality_targets"


def _duplicate_content_detail(conflicts: list[DuplicateIngestConflict]) -> dict[str, Any]:
    return {
        "code": _DUPLICATE_CONTENT_CODE,
        "message": _DUPLICATE_CONTENT_MESSAGE,
        "conflicts": [IngestUploadService.duplicate_conflict_payload(c) for c in conflicts],
    }


def _resolve_ingestion_instructions(raw: str | None) -> str | None:
    settings = get_settings()
    result = sanitize_ingestion_instructions(
        raw,
        max_length=settings.ingestion_instructions_max_length,
    )
    if result.rejected:
        raise HTTPException(
            status_code=422,
            detail={
                "code": _INVALID_INGESTION_INSTRUCTIONS_CODE,
                "message": result.rejection_reason or "Invalid ingestion instructions",
            },
        )
    return result.text


def _resolve_optional_positive_int(
    raw: str | None,
    *,
    field_name: str,
) -> int | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": _INVALID_CARDINALITY_TARGETS_CODE,
                "message": f"{field_name} must be an integer",
            },
        ) from exc
    if value <= 0:
        raise HTTPException(
            status_code=422,
            detail={
                "code": _INVALID_CARDINALITY_TARGETS_CODE,
                "message": f"{field_name} must be a positive integer",
            },
        )
    return value


@router.post("/ingest", status_code=202)
async def start_ingest(
    request: Request,
    files: list[UploadFile] = File(
        ...,
        description="One or more source files (PDF/PPTX/DOCX/audio/video); max 10",
    ),
    titles: str | None = Form(
        None,
        description='Optional JSON array of titles, one per file in upload order (e.g. ["BRAC SOP","UHIS guide"])',
    ),
    fuse_sources: bool = Form(
        False,
        description="After all per-file pipelines complete, run cross-source fusion (requires ≥2 files)",
    ),
    skip_merge: bool = Form(
        False,
        description="When true, Stage D does not merge new cards into existing modules",
    ),
    content_domain: str = Form(
        "clinical",
        description="digital | clinical | clinical_with_app_workflows",
    ),
    assessment_mode: str = Form(
        "with_quiz",
        description="with_quiz | read_only (read_only skips post-publish quiz generation)",
    ),
    override_duplicates: str | None = Form(
        None,
        description=(
            "Optional JSON array of booleans, one per file in upload order. When true, "
            "re-ingest even if the file's content_sha256 matches an already-ingested "
            "source_document."
        ),
    ),
    ingestion_instructions: str | None = Form(
        None,
        description="Optional steering text for Stage C module identification (batch-wide)",
    ),
    cards_per_module: str | None = Form(
        None,
        description=(
            "Optional fixed number of cards per module for this ingest "
            "(deployment bounds apply; default from settings)"
        ),
    ),
    quizzes_per_module: str | None = Form(
        None,
        description=(
            "Optional fixed number of quiz questions per module for this ingest "
            "(deployment bounds apply; default from settings)"
        ),
    ),
    sync_published_visible: str | None = Form(
        None,
        description=(
            "Optional JSON array of booleans, one per file in upload order. When true, "
            "the source document may appear in GET /sync/source-documents/published."
        ),
    ),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> dict[str, Any]:
    """Upload one or more source documents and kick off the ingestion pipeline."""
    _ = resolve_tenant_id_for_admin(request, tenant_id)
    target_cards = _resolve_optional_positive_int(cards_per_module, field_name="cards_per_module")
    target_quizzes = _resolve_optional_positive_int(quizzes_per_module, field_name="quizzes_per_module")
    upload_svc = IngestUploadService(db, storage)
    try:
        upload_svc.validate_file_count(files)
        sanitized_instructions = _resolve_ingestion_instructions(ingestion_instructions)
        upload_svc.validate_cardinality_targets(
            target_cards_per_module=target_cards,
            target_quizzes_per_module=target_quizzes,
        )
        if fuse_sources and len(files) < 2:
            raise IngestValidationError("fuse_sources requires at least 2 files")

        resolved_titles = upload_svc.resolve_titles_for_files(titles, files)
        override_flags = upload_svc.resolve_override_duplicates_for_files(override_duplicates, files)
        sync_published_visible_flags = upload_svc.resolve_sync_published_visible_for_files(
            sync_published_visible,
            files,
        )
        upload_svc.validate_ingest_metadata(
            content_domain=content_domain,
            assessment_mode=assessment_mode,
        )
        uploaded_by = resolve_spice_actor(request)
        params = IngestUploadParams(
            content_domain=content_domain,
            assessment_mode=assessment_mode,
            uploaded_by=uploaded_by,
            ingestion_instructions=sanitized_instructions,
            target_cards_per_module=target_cards,
            target_quizzes_per_module=target_quizzes,
        )
        outcomes = await upload_svc.ingest_uploaded_files(
            files=files,
            titles=resolved_titles,
            params=params,
            override_flags=override_flags,
            sync_published_visible_flags=sync_published_visible_flags,
        )
    except IngestValidationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    ingested = [outcome.ingested for outcome in outcomes if outcome.ingested is not None]
    skipped_duplicates = [outcome.skipped for outcome in outcomes if outcome.skipped is not None]

    if not ingested:
        raise HTTPException(
            status_code=409,
            detail=_duplicate_content_detail(skipped_duplicates),
        )
    if fuse_sources and len(ingested) < 2:
        raise HTTPException(
            status_code=400,
            detail="fuse_sources requires at least 2 successfully ingested files",
        )

    await db.commit()

    enqueue_thumbnail_and_batch(
        ingested,
        skip_merge=skip_merge,
        fuse_sources=fuse_sources,
    )

    response: dict[str, Any] = {
        "status": "batch_queued",
        "fuse_sources": fuse_sources,
        "skip_merge": skip_merge,
        "sources": [
            {
                "source_document_id": str(result.source_document_id),
                "title": result.title,
                "source_type": result.source_type,
                "stored_path": result.stored_path,
                "poll_url": f"/admin/ingest/by-document/{result.source_document_id}",
            }
            for result in ingested
        ],
    }
    if skipped_duplicates:
        response["skipped_duplicates"] = [
            IngestUploadService.duplicate_conflict_payload(conflict) for conflict in skipped_duplicates
        ]
    if fuse_sources:
        response["note"] = (
            "Pipelines and cross-source fusion run on the Celery worker after all uploads complete. "
            "Poll each source's poll_url for per-document progress; inspect fused modules via the admin module list."
        )
    return response


@router.get("/ingest/by-document/{source_document_id}")
async def get_ingest_status_by_document(
    source_document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> dict[str, Any]:
    """Look up the most recent ingestion_run for this source_document."""
    state = RunStateService(db)
    presenter = IngestionRunPresenter(db)
    run = await state.find_best_poll_run(source_document_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no ingestion_run found for this source_document")
    payload = await presenter.present_poll(run)
    fusion_run = await state.find_active_fusion_run_for_document(source_document_id)
    if fusion_run is not None and fusion_run.id != run.id:
        payload["cross_source_fusion"] = await presenter.present_poll(fusion_run)
    payload.update(await thumbnail_poll_fields(db, source_document_id, storage))
    return payload
