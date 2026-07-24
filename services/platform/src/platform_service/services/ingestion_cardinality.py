"""Resolve per-ingest card/quiz cardinality targets from source_document rows."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.models.source_document import SourceDocument

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionCardinality:
    """Resolved ingest-time fixed targets (None = use deployment defaults)."""

    target_cards: int | None
    target_quizzes: int | None

    def card_bounds(self, settings: Settings | None = None) -> tuple[int, int]:
        s = settings or get_settings()
        if self.target_cards is not None:
            return self.target_cards, self.target_cards
        return s.card_min_count, s.card_max_count

    def quiz_bounds(self, settings: Settings | None = None) -> tuple[int, int]:
        s = settings or get_settings()
        if self.target_quizzes is not None:
            return self.target_quizzes, self.target_quizzes
        return s.quiz_min_questions, s.quiz_max_questions

    def has_target_cards(self) -> bool:
        return self.target_cards is not None

    def has_target_quizzes(self) -> bool:
        return self.target_quizzes is not None


def _resolve_single_target(values: list[int | None], *, field_name: str) -> int | None:
    """Pick one target when multiple source documents are in scope."""
    non_null = [v for v in values if v is not None]
    if not non_null:
        return None
    distinct = set(non_null)
    if len(distinct) > 1:
        logger.warning(
            "Conflicting %s across source documents %s; falling back to deployment default",
            field_name,
            sorted(distinct),
        )
        return None
    return non_null[0]


def resolve_from_source_documents(documents: list[SourceDocument]) -> IngestionCardinality:
    """Resolve cardinality from one or more loaded source_document rows."""
    return IngestionCardinality(
        target_cards=_resolve_single_target(
            [doc.target_cards_per_module for doc in documents],
            field_name="target_cards_per_module",
        ),
        target_quizzes=_resolve_single_target(
            [doc.target_quizzes_per_module for doc in documents],
            field_name="target_quizzes_per_module",
        ),
    )


def source_document_ids_from_provenance(source_provenance: list[dict[str, Any]]) -> list[uuid.UUID]:
    """Collect unique source_document_id values from a candidate provenance list."""
    seen: set[uuid.UUID] = set()
    ordered: list[uuid.UUID] = []
    for entry in source_provenance:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("source_document_id")
        if raw is None:
            continue
        try:
            doc_id = uuid.UUID(str(raw))
        except ValueError:
            continue
        if doc_id not in seen:
            seen.add(doc_id)
            ordered.append(doc_id)
    return ordered


async def resolve_for_candidate(
    candidate_dict: dict[str, Any],
    session: AsyncSession,
) -> IngestionCardinality:
    """Load source documents cited by a candidate and resolve ingest targets."""
    provenance = candidate_dict.get("source_provenance") or []
    doc_ids = source_document_ids_from_provenance(provenance)
    if not doc_ids:
        return IngestionCardinality(target_cards=None, target_quizzes=None)
    result = await session.execute(select(SourceDocument).where(SourceDocument.id.in_(doc_ids)))
    documents = list(result.scalars().all())
    return resolve_from_source_documents(documents)


__all__ = [
    "IngestionCardinality",
    "resolve_for_candidate",
    "resolve_from_source_documents",
    "source_document_ids_from_provenance",
]
