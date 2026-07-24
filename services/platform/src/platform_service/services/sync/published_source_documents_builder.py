"""Build presigned source-document payloads for published modules."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from mc_contracts.sync import (
    PublishedSourceDocumentPayload,
    PublishedSourceDocumentsBundle,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.card_normalisation import card_row_to_dict
from platform_service.services.card_provenance import resolve_card_provenance
from platform_service.services.object_storage import ObjectStorageClient
from platform_service.services.sync.presign_service import SyncPresignService


class PublishedSourceDocumentsBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._presign = SyncPresignService(session)

    async def build(
        self,
        *,
        storage: ObjectStorageClient,
        domain: str | None = None,
        limit: int = 200,
        offset: int = 0,
        settings: Settings | None = None,
    ) -> PublishedSourceDocumentsBundle:
        """Return presigned URLs for source documents linked to published modules."""
        modules = await ModuleRepository(self._session).list_modules(
            status="published",
            latest_version_only=True,
            domain=domain,
            limit=limit,
            offset=offset,
        )

        doc_ids: list[UUID] = []
        seen_doc_ids: set[UUID] = set()

        module_ids = [module.id for module in modules]
        cards_by_module_id: dict[UUID, list[dict]] = {}
        if module_ids:
            card_rows = await ModuleRepository(self._session).list_cards_for_module_ids(module_ids)
            for row in card_rows:
                if row.module_id is None:
                    continue
                cards_by_module_id.setdefault(row.module_id, []).append(card_row_to_dict(row))

        for module in modules:
            cards = cards_by_module_id.get(module.id, [])
            if cards:
                context = await resolve_card_provenance(self._session, cards, storage=None)
                for row in context.provenance_by_block.values():
                    if row.source_document_id not in seen_doc_ids:
                        seen_doc_ids.add(row.source_document_id)
                        doc_ids.append(row.source_document_id)

            for doc_id in module.source_document_ids or []:
                if doc_id not in seen_doc_ids:
                    seen_doc_ids.add(doc_id)
                    doc_ids.append(doc_id)

        visible_doc_ids: list[UUID] = []
        if doc_ids:
            docs = await SourceRepository(self._session).list_source_documents_by_ids(doc_ids)
            visible_ids = {doc.id for doc in docs if doc.sync_published_visible}
            visible_doc_ids = [doc_id for doc_id in doc_ids if doc_id in visible_ids]

        presigned_by_doc: dict[UUID, str | None] = {}
        presigned_expires_by_doc: dict[UUID, int | None] = {}
        missing_ids: list[UUID] = []

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

        thumb_by_id = {}
        if visible_doc_ids:
            thumb_presign = await self._presign.get_source_document_thumbnail_presigned_urls(
                source_document_ids=visible_doc_ids,
                storage=storage,
                settings=settings,
            )
            thumb_by_id = {entry.source_document_id: entry for entry in thumb_presign.urls}

        docs = await SourceRepository(self._session).list_source_documents_by_ids(visible_doc_ids)
        doc_by_id = {doc.id: doc for doc in docs}

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
