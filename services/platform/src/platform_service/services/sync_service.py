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
    AssignedVideosBundle,
    ChatFaqsSyncBundle,
    ConfigSyncBundle,
    GapsSyncBundle,
    ModulesSyncBundle,
    ModuleThumbnailsPresignResponse,
    PublishedSourceDocumentsBundle,
    SourceDocumentsPresignResponse,
    SourceDocumentThumbnailsPresignResponse,
    TriggersSyncBundle,
)
from mc_foundation.objectstore import ObjectStore
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings
from platform_service.services.sync.assigned_videos_builder import AssignedVideosBuilder
from platform_service.services.sync.chat_faqs_bundle_builder import ChatFaqsBundleBuilder
from platform_service.services.sync.config_bundle_builder import ConfigBundleBuilder
from platform_service.services.sync.gaps_bundle_builder import GapsBundleBuilder
from platform_service.services.sync.modules_bundle_builder import ModulesBundleBuilder
from platform_service.services.sync.presign_service import SyncPresignService
from platform_service.services.sync.published_source_documents_builder import (
    PublishedSourceDocumentsBuilder,
)
from platform_service.services.sync.triggers_bundle_builder import TriggersBundleBuilder


class SyncService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._presign = SyncPresignService(session)
        self._config = ConfigBundleBuilder(session)
        self._modules = ModulesBundleBuilder(session)
        self._published_source_documents = PublishedSourceDocumentsBuilder(session)
        self._assigned_videos = AssignedVideosBuilder(session)
        self._triggers = TriggersBundleBuilder(session)
        self._gaps = GapsBundleBuilder(session)
        self._chat_faqs = ChatFaqsBundleBuilder(session)

    async def get_source_document_presigned_urls(
        self,
        *,
        source_document_ids: list[UUID],
        storage: ObjectStore,
        settings: Settings | None = None,
        tenant_id: UUID | None = None,
    ) -> SourceDocumentsPresignResponse:
        return await self._presign.get_source_document_presigned_urls(
            source_document_ids=source_document_ids,
            storage=storage,
            settings=settings,
            tenant_id=tenant_id,
        )

    async def get_source_document_thumbnail_presigned_urls(
        self,
        *,
        source_document_ids: list[UUID],
        storage: ObjectStore,
        settings: Settings | None = None,
        tenant_id: UUID | None = None,
    ) -> SourceDocumentThumbnailsPresignResponse:
        return await self._presign.get_source_document_thumbnail_presigned_urls(
            source_document_ids=source_document_ids,
            storage=storage,
            settings=settings,
            tenant_id=tenant_id,
        )

    async def get_module_thumbnail_presigned_urls(
        self,
        *,
        module_ids: list[UUID],
        storage: ObjectStore,
        settings: Settings | None = None,
        tenant_id: UUID | None = None,
    ) -> ModuleThumbnailsPresignResponse:
        return await self._presign.get_module_thumbnail_presigned_urls(
            module_ids=module_ids,
            storage=storage,
            settings=settings,
            tenant_id=tenant_id,
        )

    async def get_config_bundle(self) -> ConfigSyncBundle:
        return await self._config.build()

    async def get_modules_bundle(
        self,
        *,
        since: datetime,
        tenant_id: UUID | None = None,
        user_id: int | None = None,
        organization_ids: list[int] | None = None,
    ) -> ModulesSyncBundle:
        return await self._modules.build(
            since=since,
            tenant_id=tenant_id,
            user_id=user_id,
            organization_ids=organization_ids,
        )

    async def get_published_source_documents_bundle(
        self,
        *,
        storage: ObjectStore,
        domain: str | None = None,
        limit: int = 200,
        offset: int = 0,
        settings: Settings | None = None,
    ) -> PublishedSourceDocumentsBundle:
        return await self._published_source_documents.build(
            storage=storage,
            domain=domain,
            limit=limit,
            offset=offset,
            settings=settings,
        )

    async def get_assigned_videos_bundle(
        self,
        *,
        user_id: int,
        storage: ObjectStore,
        organization_ids: list[int] | None = None,
        limit: int = 50,
        offset: int = 0,
        settings: Settings | None = None,
    ) -> AssignedVideosBundle:
        return await self._assigned_videos.build(
            user_id=user_id,
            storage=storage,
            organization_ids=organization_ids,
            limit=limit,
            offset=offset,
            settings=settings,
        )

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

    async def get_chat_faqs_bundle(
        self,
        *,
        since: datetime,
        tenant_id: UUID | None = None,
    ) -> ChatFaqsSyncBundle:
        return await self._chat_faqs.build(since=since, tenant_id=tenant_id)
