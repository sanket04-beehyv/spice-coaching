"""Card source-page provenance resolution for admin and sync payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from mc_contracts.admin_modules import CardSourcePageRef
from mc_foundation.objectstore import ObjectStore
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.sync.presign_service import SyncPresignService


@dataclass(frozen=True)
class BlockProvenanceRow:
    """Provenance for one source block: page, document, and optional AV time range."""

    page_number: int
    source_document_id: UUID
    start_ms: int | None
    end_ms: int | None


@dataclass(frozen=True)
class CardProvenanceContext:
    """Batch-resolved provenance and presign data for module cards."""

    provenance_by_block: dict[UUID, BlockProvenanceRow]
    source_type_by_doc: dict[UUID, str]
    presigned_urls: dict[UUID, str | None]
    presigned_expires: dict[UUID, int | None]


def block_ids_from_card(card: dict[str, Any]) -> list[UUID]:
    ids: list[UUID] = []
    for raw in card.get("source_block_ids") or []:
        try:
            ids.append(UUID(str(raw)))
        except (TypeError, ValueError):
            continue
    return ids


def resolve_source_pages_for_blocks(
    block_ids: list[UUID],
    provenance_by_block: dict[UUID, BlockProvenanceRow],
) -> list[CardSourcePageRef]:
    """Map block ids to deduplicated page refs, preserving ``block_ids`` order."""
    seen: set[tuple[UUID, int]] = set()
    pages: list[CardSourcePageRef] = []
    for block_id in block_ids:
        row = provenance_by_block.get(block_id)
        if row is None:
            continue
        key = (row.source_document_id, row.page_number)
        if key in seen:
            continue
        seen.add(key)
        pages.append(
            CardSourcePageRef(
                source_document_id=row.source_document_id,
                page_number=row.page_number,
                start_ms=row.start_ms,
                end_ms=row.end_ms,
            )
        )
    return pages


def _presigned_url_at_page(
    base_url: str | None,
    page_number: int,
    *,
    source_type: str | None,
) -> str | None:
    if not base_url:
        return None
    if source_type == "pdf":
        return f"{base_url}#page={page_number}"
    return base_url


def render_card_source_pages(
    pages: list[CardSourcePageRef],
    *,
    source_type_by_doc: dict[UUID, str],
    presigned_by_doc: dict[UUID, str | None] | None = None,
    presigned_expires_by_doc: dict[UUID, int | None] | None = None,
) -> list[dict[str, Any]]:
    presigned_by_doc = presigned_by_doc or {}
    presigned_expires_by_doc = presigned_expires_by_doc or {}
    payload: list[dict[str, Any]] = []
    for page in pages:
        doc_id = page.source_document_id
        base_url = presigned_by_doc.get(doc_id)
        payload.append(
            page.model_copy(
                update={
                    "presigned_url": _presigned_url_at_page(
                        base_url,
                        page.page_number,
                        source_type=source_type_by_doc.get(doc_id),
                    ),
                    "presigned_expires_seconds": presigned_expires_by_doc.get(doc_id),
                }
            ).model_dump(mode="json")
        )
    return payload


def render_card_provenance(
    block_ids: list[UUID],
    context: CardProvenanceContext,
) -> list[dict[str, Any]]:
    return render_card_source_pages(
        resolve_source_pages_for_blocks(block_ids, context.provenance_by_block),
        source_type_by_doc=context.source_type_by_doc,
        presigned_by_doc=context.presigned_urls,
        presigned_expires_by_doc=context.presigned_expires,
    )


async def resolve_card_provenance(
    session: AsyncSession,
    cards: list[dict[str, Any]],
    *,
    storage: ObjectStore | None = None,
    presigned_by_doc: dict[UUID, str | None] | None = None,
    presigned_expires_by_doc: dict[UUID, int | None] | None = None,
) -> CardProvenanceContext:
    all_block_ids: list[UUID] = []
    for card in cards:
        all_block_ids.extend(block_ids_from_card(card))

    provenance_by_block: dict[UUID, BlockProvenanceRow] = {}
    if all_block_ids:
        rows = await SourceRepository(session).list_block_provenance_by_ids(all_block_ids)
        provenance_by_block = {
            block_id: BlockProvenanceRow(
                page_number=page_number,
                source_document_id=doc_id,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            for block_id, page_number, doc_id, start_ms, end_ms in rows
        }

    source_type_by_doc: dict[UUID, str] = {}
    doc_ids_in_cards = {row.source_document_id for row in provenance_by_block.values()}
    if doc_ids_in_cards:
        docs = await SourceRepository(session).list_source_documents_by_ids(list(doc_ids_in_cards))
        source_type_by_doc = {doc.id: doc.source_type for doc in docs}

    presigned_urls = dict(presigned_by_doc or {})
    presigned_expires = dict(presigned_expires_by_doc or {})
    # presigned_by_doc={doc_id: ""} skips re-presign; "" is not None so it stays out of missing_doc_ids.
    missing_doc_ids = [
        doc_id
        for doc_id in doc_ids_in_cards
        if doc_id not in presigned_urls or presigned_urls.get(doc_id) is None
    ]
    if storage is not None and missing_doc_ids:
        presign = await SyncPresignService(session).get_source_document_presigned_urls(
            source_document_ids=missing_doc_ids,
            storage=storage,
        )
        for entry in presign.urls:
            presigned_urls[entry.source_document_id] = entry.presigned_url
            presigned_expires[entry.source_document_id] = entry.expires_seconds

    return CardProvenanceContext(
        provenance_by_block=provenance_by_block,
        source_type_by_doc=source_type_by_doc,
        presigned_urls=presigned_urls,
        presigned_expires=presigned_expires,
    )
