"""Scenario sync service — builds ScenarioSyncBundle for device sync endpoint.

Devices (Android SDK) call /scenarios/sync?since_version=N to pull:
  - validated scenarios with version > N (excluding soft-deleted)
  - tombstone lists for removed scenarios / quizzes
  - associated validated quiz questions
  - ``current_version`` watermark for the next sync cursor
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from mc_contracts.sync import (
    ConfigSyncBundle,
    GapsSyncBundle,
    ModulesSyncBundle,
    ModuleThumbnailsPresignResponse,
    SourceDocumentsPresignResponse,
    SourceDocumentThumbnailsPresignResponse,
    TriggersSyncBundle,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings
from platform_service.services.object_storage import ObjectStorageClient
from platform_service.services.sync.config_bundle_builder import ConfigBundleBuilder
from platform_service.services.sync.gaps_bundle_builder import GapsBundleBuilder
from platform_service.services.sync.modules_bundle_builder import ModulesBundleBuilder
from platform_service.services.sync.presign_service import SyncPresignService
from platform_service.services.sync.triggers_bundle_builder import TriggersBundleBuilder


class SyncService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._presign = SyncPresignService(session)
        self._config = ConfigBundleBuilder(session)
        self._modules = ModulesBundleBuilder(session)
        self._triggers = TriggersBundleBuilder(session)
        self._gaps = GapsBundleBuilder(session)

    async def get_source_document_presigned_urls(
        self,
        *,
        source_document_ids: list[UUID],
        storage: ObjectStorageClient,
        settings: Settings | None = None,
    ) -> SourceDocumentsPresignResponse:
        return await self._presign.get_source_document_presigned_urls(
            source_document_ids=source_document_ids,
            storage=storage,
            settings=settings,
        )

    async def get_source_document_thumbnail_presigned_urls(
        self,
        *,
        source_document_ids: list[UUID],
        storage: ObjectStorageClient,
        settings: Settings | None = None,
    ) -> SourceDocumentThumbnailsPresignResponse:
        return await self._presign.get_source_document_thumbnail_presigned_urls(
            source_document_ids=source_document_ids,
            storage=storage,
            settings=settings,
        )

    async def get_module_thumbnail_presigned_urls(
        self,
        *,
        module_ids: list[UUID],
        storage: ObjectStorageClient,
        settings: Settings | None = None,
    ) -> ModuleThumbnailsPresignResponse:
        return await self._presign.get_module_thumbnail_presigned_urls(
            module_ids=module_ids,
            storage=storage,
            settings=settings,
        )

    async def get_config_bundle(self) -> ConfigSyncBundle:
        return await self._config.build()

    async def get_modules_bundle(
        self,
        *,
        since: datetime,
        tenant_id: UUID | None = None,
    ) -> ModulesSyncBundle:
        return await self._modules.build(since=since, tenant_id=tenant_id)

    async def get_triggers_bundle(
        self,
        *,
        since: datetime,
        tenant_id: UUID | None = None,
    ) -> TriggersSyncBundle:
        return await self._triggers.build(since=since, tenant_id=tenant_id)

    async def get_gaps_bundle(
        self,
        *,
        since: datetime | None,
        chw_id: int | None,
        tenant_id: UUID | None = None,
    ) -> GapsSyncBundle:
        return await self._gaps.build(since=since, chw_id=chw_id, tenant_id=tenant_id)
