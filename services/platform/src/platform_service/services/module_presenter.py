"""Shared module payload shaping for admin dashboard endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from mc_contracts.admin_modules import (
    CardSourcePageRef,
    ModuleSourceDocumentRef,
    ModuleSummary,
    QuizQuestionPayload,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.object_storage import ObjectStorageClient
from platform_service.services.source_thumbnail_service import presign_thumbnail
from platform_service.services.sync_service import SyncService


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


async def summary_from_module(
    module: Module,
    *,
    card_count: int,
    quiz_count: int,
    storage: ObjectStorageClient | None = None,
) -> ModuleSummary:
    thumb_path = module.thumbnail_storage_path
    thumb_url: str | None = None
    thumb_expires: int | None = None
    if storage is not None:
        thumb_presign = await presign_thumbnail(storage, thumbnail_storage_path=thumb_path)
        if thumb_presign:
            thumb_url, thumb_expires = thumb_presign
    return ModuleSummary(
        id=module.id,
        module_family_id=module.module_family_id,
        version=module.version,
        title_bn=module.title_bn,
        title_en=module.title_en,
        description_bn=module.description_bn,
        domain=module.domain,
        module_type=module.module_type,
        lifecycle_status=module.lifecycle_status,
        clinically_reviewed=module.clinically_reviewed,
        has_visibility_window=module.visibility_window is not None,
        card_count=card_count,
        quiz_count=quiz_count,
        estimated_minutes=module.estimated_minutes,
        published_at=module.published_at,
        created_at=module.created_at,
        quality_flags=module.quality_flags_jsonb,
        thumbnail_storage_path=thumb_path,
        thumbnail_presigned_url=thumb_url,
        thumbnail_presigned_expires_seconds=thumb_expires,
    )


def _block_ids_from_card(card: dict[str, Any]) -> list[UUID]:
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
    storage: ObjectStorageClient | None = None,
    presigned_by_doc: dict[UUID, str | None] | None = None,
    presigned_expires_by_doc: dict[UUID, int | None] | None = None,
) -> CardProvenanceContext:
    all_block_ids: list[UUID] = []
    for card in cards:
        all_block_ids.extend(_block_ids_from_card(card))

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
        presign = await SyncService(session).get_source_document_presigned_urls(
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


async def cards_with_source_pages(
    session: AsyncSession,
    cards: list[dict[str, Any]],
    *,
    storage: ObjectStorageClient | None = None,
    presigned_by_doc: dict[UUID, str | None] | None = None,
    presigned_expires_by_doc: dict[UUID, int | None] | None = None,
) -> list[dict[str, Any]]:
    """Return card dicts enriched with ``source_pages`` resolved from ``source_block_ids``.

    Each ``source_pages`` entry includes ``presigned_url`` = document presigned GET
    URL + ``#page={page_number}`` for PDF deep-linking when object storage presign succeeds.
    """
    if not cards:
        return []

    context = await resolve_card_provenance(
        session,
        cards,
        storage=storage,
        presigned_by_doc=presigned_by_doc,
        presigned_expires_by_doc=presigned_expires_by_doc,
    )

    card_block_ids = [_block_ids_from_card(card) for card in cards]
    enriched: list[dict[str, Any]] = []
    for card, block_ids in zip(cards, card_block_ids, strict=True):
        payload = dict(card)
        payload["source_pages"] = render_card_provenance(block_ids, context)
        enriched.append(payload)
    return enriched


def quiz_payload(rows: list[ModuleQuizQuestion]) -> list[QuizQuestionPayload]:
    return [
        QuizQuestionPayload(
            id=r.id,
            question_order=r.question_order,
            question_bn=r.question_bn,
            question_en=r.question_en,
            case_setup_bn=r.case_setup_bn,
            case_setup_en=r.case_setup_en,
            options_bn=list(r.options_bn or []),
            options_en=list(r.options_en) if r.options_en else None,
            correct_indices=list(r.correct_indices or []),
            explanation_bn=r.explanation_bn,
            explanation_en=r.explanation_en,
            difficulty=r.difficulty,
        )
        for r in rows
    ]


async def source_documents_for_module(
    session: AsyncSession,
    module: Module,
    storage: ObjectStorageClient,
) -> list[ModuleSourceDocumentRef]:
    doc_ids = list(module.source_document_ids or [])
    if not doc_ids:
        return []

    presign = await SyncService(session).get_source_document_presigned_urls(
        source_document_ids=doc_ids,
        storage=storage,
    )
    url_by_id = {entry.source_document_id: entry for entry in presign.urls}
    docs = await SourceRepository(session).list_source_documents_by_ids(doc_ids)
    doc_by_id = {doc.id: doc for doc in docs}
    refs: list[ModuleSourceDocumentRef] = []
    for doc_id in doc_ids:
        doc = doc_by_id.get(doc_id)
        thumb_path = doc.thumbnail_storage_path if doc is not None else None
        thumb_presign = await presign_thumbnail(storage, thumbnail_storage_path=thumb_path)
        refs.append(
            ModuleSourceDocumentRef(
                source_document_id=doc_id,
                presigned_url=url_by_id[doc_id].presigned_url if doc_id in url_by_id else None,
                presigned_expires_seconds=url_by_id[doc_id].expires_seconds if doc_id in url_by_id else None,
                thumbnail_storage_path=thumb_path,
                thumbnail_presigned_url=thumb_presign[0] if thumb_presign else None,
                thumbnail_presigned_expires_seconds=thumb_presign[1] if thumb_presign else None,
            )
        )
    return refs


async def get_quiz_counts(session: AsyncSession, module_ids: list[UUID]) -> dict[UUID, int]:
    if not module_ids:
        return {}
    stmt = (
        select(ModuleQuizQuestion.module_id, func.count(ModuleQuizQuestion.id))
        .where(ModuleQuizQuestion.module_id.in_(module_ids))
        .group_by(ModuleQuizQuestion.module_id)
    )
    result = await session.execute(stmt)
    return {r[0]: r[1] for r in result.all()}


def visibility_window_bounds(module: Module) -> tuple[datetime | None, datetime | None]:
    if module.visibility_window is None:
        return None, None
    return (
        getattr(module.visibility_window, "lower", None),
        getattr(module.visibility_window, "upper", None),
    )
