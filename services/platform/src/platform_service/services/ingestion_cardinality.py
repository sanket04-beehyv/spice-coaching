"""Resolve per-ingest card/quiz cardinality targets from ingest_batch."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import Settings, get_settings
from platform_service.db.models.ingest_batch import IngestBatch
from platform_service.db.models.ingestion_run import IngestionRun


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


def resolve_from_batch(batch: IngestBatch | None) -> IngestionCardinality:
    """Resolve cardinality from an ingest_batch row."""
    if batch is None:
        return IngestionCardinality(target_cards=None, target_quizzes=None)
    return IngestionCardinality(
        target_cards=batch.cards_per_module,
        target_quizzes=batch.quizzes_per_module,
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


async def load_batch_for_run(
    session: AsyncSession,
    run_id: uuid.UUID | None,
) -> IngestBatch | None:
    """Load the ingest_batch linked to an ingestion_run, if any."""
    if run_id is None:
        return None
    run = await session.get(IngestionRun, run_id)
    if run is None or run.ingest_batch_id is None:
        return None
    return await session.get(IngestBatch, run.ingest_batch_id)


async def resolve_for_run(
    session: AsyncSession,
    run_id: uuid.UUID | None,
) -> IngestionCardinality:
    """Resolve cardinality from the batch linked to an ingestion run."""
    batch = await load_batch_for_run(session, run_id)
    return resolve_from_batch(batch)


async def resolve_for_candidate(
    candidate_dict: dict[str, Any],
    session: AsyncSession,
) -> IngestionCardinality:
    """Resolve ingest targets from the candidate's ingestion_run batch."""
    raw_run_id = candidate_dict.get("ingestion_run_id")
    if raw_run_id is None:
        return IngestionCardinality(target_cards=None, target_quizzes=None)
    try:
        run_id = uuid.UUID(str(raw_run_id))
    except ValueError:
        return IngestionCardinality(target_cards=None, target_quizzes=None)
    return await resolve_for_run(session, run_id)


__all__ = [
    "IngestionCardinality",
    "load_batch_for_run",
    "resolve_for_candidate",
    "resolve_for_run",
    "resolve_from_batch",
    "source_document_ids_from_provenance",
]
