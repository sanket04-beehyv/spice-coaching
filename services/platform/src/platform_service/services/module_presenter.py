"""Shared module payload shaping for admin dashboard endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from mc_contracts.admin_modules import (
    ModuleSourceDocumentRef,
    ModuleSummary,
    QuizQuestionPayload,
)
from mc_foundation.objectstore import ObjectStore
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.models.module_card import ModuleCard
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.card_normalisation import card_row_to_dict
from platform_service.services.card_provenance import (
    BlockProvenanceRow,
    CardProvenanceContext,
    _presigned_url_at_page,
    block_ids_from_card,
    render_card_provenance,
    render_card_source_pages,
    resolve_card_provenance,
    resolve_source_pages_for_blocks,
)
from platform_service.services.source_thumbnail_service import presign_thumbnail
from platform_service.services.sync_service import SyncService

# Re-export provenance helpers for existing callers and tests.
__all__ = [
    "BlockProvenanceRow",
    "CardProvenanceContext",
    "_presigned_url_at_page",
    "card_payload",
    "cards_with_source_pages",
    "get_card_counts",
    "get_quiz_counts",
    "quiz_payload",
    "render_card_provenance",
    "render_card_source_pages",
    "resolve_card_provenance",
    "resolve_source_pages_for_blocks",
    "source_documents_for_module",
    "summary_from_module",
    "visibility_window_bounds",
]


async def summary_from_module(
    module: Module,
    *,
    card_count: int,
    quiz_count: int,
    storage: ObjectStore | None = None,
    family: ModuleFamily | None = None,
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
        title=module.title_localized,
        description=module.description_localized,
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
        first_activated_at=module.first_activated_at,
        last_deactivated_at=module.last_deactivated_at,
        last_reactivated_at=module.last_reactivated_at,
        quality_flags=module.quality_flags_jsonb,
        search_metadata=module.search_metadata_jsonb,
        chatbot_faqs_only=module.chatbot_faqs_only,
        thumbnail_storage_path=thumb_path,
        thumbnail_presigned_url=thumb_url,
        thumbnail_presigned_expires_seconds=thumb_expires,
        source_document_ids=([str(doc_id) for doc_id in (module.source_document_ids or [])] or None),
        merge_secondary_module_id=module.merge_secondary_module_id,
        merge_primary_module_id=module.merge_primary_module_id,
        merge_source_module_id=module.merge_source_module_id,
    )


def card_payload(rows: list[ModuleCard]) -> list[dict[str, Any]]:
    return [card_row_to_dict(row) for row in rows]


async def cards_with_source_pages(
    session: AsyncSession,
    cards: list[dict[str, Any]],
    *,
    storage: ObjectStore | None = None,
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

    card_block_ids = [block_ids_from_card(card) for card in cards]
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
            question=r.question_localized,
            case_setup=r.case_setup_localized,
            options=r.options_localized,
            correct_indices=list(r.correct_indices or []),
            explanation=r.explanation_localized,
            difficulty=r.difficulty,
        )
        for r in rows
    ]


async def source_documents_for_module(
    session: AsyncSession,
    module: Module,
    storage: ObjectStore,
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


async def get_card_counts(session: AsyncSession, module_ids: list[UUID]) -> dict[UUID, int]:
    if not module_ids:
        return {}
    stmt = (
        select(ModuleCard.module_id, func.count(ModuleCard.id))
        .where(ModuleCard.module_id.in_(module_ids))
        .group_by(ModuleCard.module_id)
    )
    result = await session.execute(stmt)
    return {r[0]: r[1] for r in result.all()}


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
