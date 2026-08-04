"""Build presigned payloads for sync-published-visible source documents."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from mc_contracts.sync import (
    PublishedSourceDocumentPayload,
    PublishedSourceDocumentsBundle,
    SourceDocumentThumbnailPresignedUrlPayload,
)
from mc_foundation.objectstore import ObjectStore
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.sync.presign_service import SyncPresignService


class PublishedSourceDocumentsBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._presign = SyncPresignService(session)

    async def build(
        self,
        *,
        storage: ObjectStore,
        domain: str | None = None,
        limit: int = 200,
        offset: int = 0,
        settings: Settings | None = None,
    ) -> PublishedSourceDocumentsBundle:
        """Return presigned URLs for documents with ``sync_published_visible=true``."""
        docs = await SourceRepository(self._session).list_sync_published_visible_documents(
            domain=domain,
            limit=limit,
            offset=offset,
        )
        visible_doc_ids = [doc.id for doc in docs]
        doc_by_id = {doc.id: doc for doc in docs}

        presigned_by_doc: dict[UUID, str | None] = {}
        presigned_expires_by_doc: dict[UUID, int | None] = {}
        missing_ids: list[UUID] = []
        thumb_by_id: dict[UUID, SourceDocumentThumbnailPresignedUrlPayload] = {}

        if visible_doc_ids:
            doc_presign = await self._presign.get_source_document_presigned_urls(
                source_document_ids=visible_doc_ids,
                storage=storage,
                settings=settings,
            )
            for entry in doc_presign.urls:
                presigned_by_doc[entry.source_document_id] = entry.presigned_url
                presigned_expires_by_doc[entry.source_document_id] = entry.expires_seconds
            missing_ids = list(doc_presign.missing_ids)

            thumb_presign = await self._presign.get_source_document_thumbnail_presigned_urls(
                source_document_ids=visible_doc_ids,
                storage=storage,
                settings=settings,
            )
            thumb_by_id = {entry.source_document_id: entry for entry in thumb_presign.urls}

        source_documents = [
            PublishedSourceDocumentPayload(
                source_document_id=doc_id,
                title=(doc_by_id[doc_id].title if doc_id in doc_by_id else None),
                original_filename=(doc_by_id[doc_id].original_filename if doc_id in doc_by_id else None),
                presigned_url=presigned_by_doc.get(doc_id),
                presigned_expires_seconds=presigned_expires_by_doc.get(doc_id),
                thumbnail_presigned_url=(
                    thumb_by_id[doc_id].presigned_url if doc_id in thumb_by_id else None
                ),
                thumbnail_presigned_expires_seconds=(
                    thumb_by_id[doc_id].expires_seconds if doc_id in thumb_by_id else None
                ),
            )
            for doc_id in visible_doc_ids
        ]

        return PublishedSourceDocumentsBundle(
            source_documents=source_documents,
            missing_ids=missing_ids,
            server_time_utc=datetime.now(UTC).isoformat(),
        )
