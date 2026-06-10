"""Scenario and config sync endpoints — platform → Android SDK.

Canonical paths:
  GET /scenarios/sync?since_version=N → ScenarioSyncBundle
  GET /config/sync                    → ConfigSyncBundle
  POST /sync/source-documents/presigned-urls → SourceDocumentsPresignResponse
  POST /sync/source-documents/presigned-thumbnails → SourceDocumentThumbnailsPresignResponse
  POST /sync/modules/presigned-thumbnails → ModuleThumbnailsPresignResponse
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from mc_contracts.sync import (
    ConfigSyncBundle,
    GapsSyncBundle,
    ModulesSyncBundle,
    ModuleThumbnailsPresignRequest,
    ModuleThumbnailsPresignResponse,
    SourceDocumentsPresignRequest,
    SourceDocumentsPresignResponse,
    SourceDocumentThumbnailsPresignRequest,
    SourceDocumentThumbnailsPresignResponse,
    TriggersSyncBundle,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import (
    resolve_chw_id_for_device_route,
    resolve_tenant_id_for_device_route,
)
from platform_service.deps import get_db, get_object_storage_client
from platform_service.services.object_storage import ObjectStorageClient
from platform_service.services.sync_service import SyncService

router = APIRouter(prefix="/sync", tags=["sync"])


def _effective_tenant_id(request: Request, requested_tenant_id: UUID | None) -> UUID | None:
    resolved = resolve_tenant_id_for_device_route(request, requested_tenant_id)
    if resolved is None:
        return None
    if resolved == UUID(int=0):
        return None
    return resolved


@router.post("/source-documents/presigned-urls", response_model=SourceDocumentsPresignResponse)
async def sync_source_document_presigned_urls(
    body: SourceDocumentsPresignRequest,
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> SourceDocumentsPresignResponse:
    """Return presigned GET URLs for a batch of source documents (partial success)."""
    return await SyncService(db).get_source_document_presigned_urls(
        source_document_ids=body.source_document_ids,
        storage=storage,
    )


@router.post(
    "/source-documents/presigned-thumbnails",
    response_model=SourceDocumentThumbnailsPresignResponse,
)
async def sync_source_document_thumbnail_presigned_urls(
    body: SourceDocumentThumbnailsPresignRequest,
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> SourceDocumentThumbnailsPresignResponse:
    """Return presigned GET URLs for a batch of source document thumbnails (partial success)."""
    return await SyncService(db).get_source_document_thumbnail_presigned_urls(
        source_document_ids=body.source_document_ids,
        storage=storage,
    )


@router.post("/modules/presigned-thumbnails", response_model=ModuleThumbnailsPresignResponse)
async def sync_module_thumbnail_presigned_urls(
    body: ModuleThumbnailsPresignRequest,
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> ModuleThumbnailsPresignResponse:
    """Return presigned GET URLs for a batch of module thumbnails (partial success)."""
    return await SyncService(db).get_module_thumbnail_presigned_urls(
        module_ids=body.module_ids,
        storage=storage,
    )


@router.get("/config", response_model=ConfigSyncBundle)
async def sync_config(
    db: AsyncSession = Depends(get_db),
) -> ConfigSyncBundle:
    """Return current config threshold snapshot for offline device use."""
    return await SyncService(db).get_config_bundle()


@router.get("/modules", response_model=ModulesSyncBundle)
async def sync_modules(
    request: Request,
    since: datetime = Query(
        ..., description="ISO-8601 datetime; return modules updated after this timestamp"
    ),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
) -> ModulesSyncBundle:
    """Return published modules updated after `since` (plus their quiz payloads)."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    effective_tenant = _effective_tenant_id(request, tenant_id)
    return await SyncService(db).get_modules_bundle(since=since, tenant_id=effective_tenant)


@router.get("/triggers", response_model=TriggersSyncBundle)
async def sync_triggers(
    request: Request,
    since: datetime = Query(
        ...,
        description="ISO-8601 datetime; return triggers updated after this timestamp (and all bindings for them)",
    ),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
) -> TriggersSyncBundle:
    """Return trigger definitions updated after `since` plus their module-family bindings."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    effective_tenant = _effective_tenant_id(request, tenant_id)
    return await SyncService(db).get_triggers_bundle(since=since, tenant_id=effective_tenant)


@router.get("/gaps", response_model=GapsSyncBundle)
async def sync_gaps(
    request: Request,
    since: datetime | None = Query(
        default=None,
        description="ISO-8601 datetime; return gaps/state rows updated after this timestamp",
    ),
    chw_id: int | None = Query(
        default=None,
        description=(
            "Optional CHW id (integer). When provided, include per-CHW gap state, "
            "module completion state, and partial module quiz progress (incomplete questions)."
        ),
    ),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
) -> GapsSyncBundle:
    """Return behavioural gaps plus optional per-CHW state and partial quiz progress for offline sync."""
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    effective_chw_id = resolve_chw_id_for_device_route(request, chw_id)
    effective_tenant = _effective_tenant_id(request, tenant_id)
    return await SyncService(db).get_gaps_bundle(
        since=since,
        chw_id=effective_chw_id,
        tenant_id=effective_tenant,
    )
