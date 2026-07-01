"""Scenario and config sync endpoints — platform → Android SDK.

Canonical paths:
  GET /scenarios/sync?since_version=N → ScenarioSyncBundle
  GET /config/sync                    → ConfigSyncBundle
  POST /sync/source-documents/presigned-urls → SourceDocumentsPresignResponse
  POST /sync/source-documents/presigned-thumbnails → SourceDocumentThumbnailsPresignResponse
  POST /sync/modules/presigned-thumbnails → ModuleThumbnailsPresignResponse
  GET  /sync/source-documents/published   → PublishedSourceDocumentsBundle
  GET  /sync/chat-faqs?since=<ISO-8601> → ChatFaqsSyncBundle
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from mc_contracts.sync import (
    ChatFaqsSyncBundle,
    ConfigSyncBundle,
    GapsSyncBundle,
    ModulesSyncBundle,
    ModuleThumbnailsPresignRequest,
    ModuleThumbnailsPresignResponse,
    PublishedSourceDocumentsBundle,
    SourceDocumentsPresignRequest,
    SourceDocumentsPresignResponse,
    SourceDocumentThumbnailsPresignRequest,
    SourceDocumentThumbnailsPresignResponse,
    TriggersSyncBundle,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.auth.spice_identity import (
    require_chw_id_for_device_route,
    resolve_chw_id_for_device_route,
    resolve_tenant_id_for_device_route,
)
from platform_service.config import Settings, get_settings
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
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> SourceDocumentsPresignResponse:
    """Return presigned GET URLs for a batch of source documents (partial success)."""
    effective_tenant = _effective_tenant_id(request, tenant_id)
    return await SyncService(db).get_source_document_presigned_urls(
        source_document_ids=body.source_document_ids,
        storage=storage,
        tenant_id=effective_tenant,
    )


@router.post(
    "/source-documents/presigned-thumbnails",
    response_model=SourceDocumentThumbnailsPresignResponse,
)
async def sync_source_document_thumbnail_presigned_urls(
    body: SourceDocumentThumbnailsPresignRequest,
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> SourceDocumentThumbnailsPresignResponse:
    """Return presigned GET URLs for a batch of source document thumbnails (partial success)."""
    effective_tenant = _effective_tenant_id(request, tenant_id)
    return await SyncService(db).get_source_document_thumbnail_presigned_urls(
        source_document_ids=body.source_document_ids,
        storage=storage,
        tenant_id=effective_tenant,
    )


@router.post("/modules/presigned-thumbnails", response_model=ModuleThumbnailsPresignResponse)
async def sync_module_thumbnail_presigned_urls(
    body: ModuleThumbnailsPresignRequest,
    request: Request,
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
) -> ModuleThumbnailsPresignResponse:
    """Return presigned GET URLs for a batch of module thumbnails (partial success)."""
    effective_tenant = _effective_tenant_id(request, tenant_id)
    return await SyncService(db).get_module_thumbnail_presigned_urls(
        module_ids=body.module_ids,
        storage=storage,
        tenant_id=effective_tenant,
    )


@router.get("/source-documents/published", response_model=PublishedSourceDocumentsBundle)
async def sync_published_source_documents(
    domain: str | None = Query(None),
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStorageClient = Depends(get_object_storage_client),
    settings: Settings = Depends(get_settings),
) -> PublishedSourceDocumentsBundle:
    """Return presigned URLs for source documents linked to published modules.

    Fetches documents cited by published modules (not module payloads themselves).
    Module content comes from ``GET /sync/modules``.
    """
    return await SyncService(db).get_published_source_documents_bundle(
        storage=storage,
        domain=domain,
        limit=limit,
        offset=offset,
        settings=settings,
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
    user_id: int | None = Query(
        default=None,
        description="When provided, include module IDs assigned to this user.",
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
    effective_user_id = require_chw_id_for_device_route(request, user_id) if user_id is not None else None
    spice_user = getattr(request.state, "spice_user", None)
    organization_ids = spice_user.organization_ids if spice_user and effective_user_id else None

    return await SyncService(db).get_modules_bundle(
        since=since,
        tenant_id=effective_tenant,
        user_id=effective_user_id,
        organization_ids=organization_ids,
    )


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


@router.get("/chat-faqs", response_model=ChatFaqsSyncBundle)
async def sync_chat_faqs(
    request: Request,
    since: datetime = Query(..., description="ISO-8601 datetime; return FAQs updated after this timestamp"),
    tenant_id: UUID | None = Query(
        default=None,
        description="Optional tenant UUID override (admin principals only when auth is enabled).",
    ),
    db: AsyncSession = Depends(get_db),
) -> ChatFaqsSyncBundle:
    """Return ranked frequent chat questions, optionally scoped to a tenant."""
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    effective_tenant = _effective_tenant_id(request, tenant_id)
    return await SyncService(db).get_chat_faqs_bundle(since=since, tenant_id=effective_tenant)
