"""Admin v3.3 ingest endpoint — drives a real source document through the
PipelineOrchestrator (Stage A → B → C → D) and exposes status polling.

Distinct from the legacy `POST /admin/documents/upload` (scenarios pipeline
on the Document/Scenario tables); this endpoint operates on the v3.3
`source_document` + `ingestion_run` + `module_candidate_draft` tables.

Endpoints:
  POST /admin/ingest              — upload one or more files + start pipeline (optional cross-source fusion)
  GET  /admin/ingest/{run_id}    — poll run + step state + emitted candidates

Uploaded bytes are stored in MinIO under the ``ingest/`` prefix. The API
returns ``stored_path`` as ``{bucket}/{object_key}``. The pipeline downloads
the object to a temp file for Stage A extractors, then deletes the temp file.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from mc_contracts.admin_ingest import FusionRequest
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_user import resolve_spice_actor
from platform_service.deps import get_db, get_object_storage_client
from platform_service.services.ingest_enqueue_service import (
    enqueue_thumbnail,
    enqueue_thumbnail_and_batch,
)
from platform_service.services.ingest_stream_service import IngestStreamService
from platform_service.services.ingest_upload_service import (
    DuplicateIngestConflict,
    IngestUploadParams,
    IngestUploadService,
)
from platform_service.services.ingestion_run_presenter import IngestionRunPresenter
from platform_service.services.object_storage import ObjectStorageClient
from platform_service.services.run_state_service import RunStateService

router = APIRouter(
    prefix="/admin",
    tags=["admin-ingest"],
)

_DUPLICATE_CONTENT_CODE = "duplicate_content"
_DUPLICATE_CONTENT_MESSAGE = "One or more files match already-ingested content; set override to re-ingest."


def _duplicate_content_detail(conflicts: list[DuplicateIngestConflict]) -> dict[str, Any]:
    return {
        "code": _DUPLICATE_CONTENT_CODE,
        "message": _DUPLICATE_CONTENT_MESSAGE,
        "conflicts": [IngestUploadService.duplicate_conflict_payload(c) for c in conflicts],
    }


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
        description="digital | clinical | clinical_with_app_action | supervisor_update",
    ),
    assessment_mode: str = Form(
        "with_quiz",
        description="with_quiz | read_only (read_only skips post-publish quiz generation)",
    ),
    authority_label: str = Form("BRAC", description="Source authority label (e.g. 'BRAC' or 'BBS')"),
    primary_language: str = Form("bn", description="Primary language of the source: 'bn' or 'en'"),
    mode: str = Form(
        "append",
        description=(
            "Workspace handling. 'append' (default): leave existing published modules in "
            "place. 'new': retire all currently-published modules before ingesting (use "
            "this to start a clean workspace, e.g. switching from one program to another)."
        ),
    ),
    override_duplicates: str | None = Form(
        None,
        description=(
            "Optional JSON array of booleans, one per file in upload order. When true, "
            "re-ingest even if the file's content_sha256 matches an already-ingested "
            "source_document."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> dict[str, Any]:
    """Upload one or more source documents and kick off the v3.3 ingestion pipeline."""
    upload_svc = IngestUploadService(db, storage)
    upload_svc.validate_file_count(files)
    upload_svc.validate_mode(mode)
    if fuse_sources and len(files) < 2:
        raise HTTPException(
            status_code=400,
            detail="fuse_sources requires at least 2 files",
        )

    resolved_titles = upload_svc.resolve_titles_for_files(titles, files)
    override_flags = upload_svc.resolve_override_duplicates_for_files(override_duplicates, files)
    upload_svc.validate_ingest_metadata(
        content_domain=content_domain,
        assessment_mode=assessment_mode,
    )
    retired_count, retired_ids = await upload_svc.retire_published_modules_if_new(mode)
    uploaded_by = resolve_spice_actor(request)
    params = IngestUploadParams(
        content_domain=content_domain,
        assessment_mode=assessment_mode,
        authority_label=authority_label,
        primary_language=primary_language,
        uploaded_by=uploaded_by,
        retired_ids=retired_ids,
    )
    outcomes = await upload_svc.ingest_uploaded_files(
        files=files,
        titles=resolved_titles,
        params=params,
        override_flags=override_flags,
    )
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
        primary_language=primary_language,
        skip_merge=skip_merge,
        fuse_sources=fuse_sources,
    )

    response: dict[str, Any] = {
        "status": "batch_queued",
        "fuse_sources": fuse_sources,
        "skip_merge": skip_merge,
        "mode": mode,
        "modules_retired": retired_count,
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


@router.post("/ingest/stream")
async def start_ingest_stream(
    request: Request,
    file: UploadFile = File(..., description="Source file (PDF/PPTX/DOCX/audio/video)"),
    title: str = Form(..., description="Human-readable title for the source"),
    content_domain: str = Form(
        "clinical",
        description="digital | clinical | clinical_with_app_action | supervisor_update",
    ),
    assessment_mode: str = Form(
        "with_quiz",
        description="with_quiz | read_only (read_only skips post-publish quiz generation)",
    ),
    authority_label: str = Form("BRAC", description="Source authority label (e.g. 'BRAC' or 'BBS')"),
    primary_language: str = Form("bn", description="Primary language of the source: 'bn' or 'en'"),
    mode: str = Form(
        "append",
        description="Same as POST /ingest: 'append' | 'new' (retire published modules before ingest).",
    ),
    skip_merge: bool = Form(
        False,
        description="When true, Stage D does not merge new cards into existing modules",
    ),
    override_duplicate: bool = Form(
        False,
        description=(
            "When true, re-ingest even if the file's content_sha256 matches an "
            "already-ingested source_document."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> StreamingResponse:
    """Upload a source document and stream SSE pipeline progress events."""
    upload_svc = IngestUploadService(db, storage)
    upload_svc.validate_mode(mode)
    upload_svc.validate_ingest_metadata(
        content_domain=content_domain,
        assessment_mode=assessment_mode,
    )
    _, retired_ids = await upload_svc.retire_published_modules_if_new(mode)
    uploaded_by = resolve_spice_actor(request)
    outcome = await upload_svc.ingest_one_uploaded_file(
        file=file,
        title=title,
        params=IngestUploadParams(
            content_domain=content_domain,
            assessment_mode=assessment_mode,
            authority_label=authority_label,
            primary_language=primary_language,
            uploaded_by=uploaded_by,
            retired_ids=retired_ids,
        ),
        override_duplicate=override_duplicate,
    )
    if outcome.skipped is not None:
        raise HTTPException(
            status_code=409,
            detail=_duplicate_content_detail([outcome.skipped]),
        )
    ingested = outcome.ingested
    assert ingested is not None
    await db.commit()

    enqueue_thumbnail(ingested, primary_language=primary_language, skip_merge=skip_merge)

    return StreamingResponse(
        IngestStreamService.pipeline_sse_lines(
            source_document_id=ingested.source_document_id,
            source_path=ingested.stored_path,
            source_type=ingested.source_type,
            primary_language=primary_language,
            skip_merge=skip_merge,
        ),
        media_type="text/event-stream",
    )


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
    payload.update(await IngestStreamService.thumbnail_poll_fields(db, source_document_id, storage))
    return payload


@router.get("/ingest/{run_id}")
async def get_ingest_status(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    state = RunStateService(db)
    run = await state.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"ingestion_run {run_id} not found")
    return await IngestionRunPresenter(db).present_poll(run)


@router.post("/fusion", status_code=202)
async def start_fusion(
    request: FusionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Trigger a cross-source fusion pass over the specified source documents."""
    from platform_service.celery_tasks import run_cross_source_fusion_task
    from platform_service.services.run_state_service import (
        ConcurrentFusionRunError,
        ConcurrentRunError,
        RunStateService,
    )

    if len(request.source_document_ids) < 2:
        raise HTTPException(
            status_code=400,
            detail="cross-source fusion requires at least two source_document_ids",
        )
    try:
        await RunStateService(db).assert_no_active_fusion_overlap(request.source_document_ids)
    except ConcurrentRunError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"source_document {exc.source_document_id} has an active ingest run",
        ) from exc
    except ConcurrentFusionRunError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"source_document {exc.source_document_id} has an active fusion run",
        ) from exc

    run_cross_source_fusion_task.delay({"source_document_ids": [str(d) for d in request.source_document_ids]})
    return {
        "status": "fusion_queued",
        "source_document_ids": [str(d) for d in request.source_document_ids],
        "note": (
            "Fusion runs on the Celery worker. Inspect resulting modules via the admin "
            "module-list endpoint; published rows with array_length(source_document_ids) "
            ">= 2 are the fused outputs."
        ),
    }
