"""Tests for ingest-time card/quiz cardinality resolution."""

from __future__ import annotations

import uuid

from platform_service.db.models.ingest_batch import IngestBatch
from platform_service.services.ingestion_cardinality import (
    IngestionCardinality,
    resolve_from_batch,
    source_document_ids_from_provenance,
)


def _batch(
    *,
    cards: int | None = None,
    quizzes: int | None = None,
) -> IngestBatch:
    return IngestBatch(
        status="queued",
        cards_per_module=cards,
        quizzes_per_module=quizzes,
    )


class TestResolveFromBatch:
    def test_none_batch_returns_none_targets(self) -> None:
        result = resolve_from_batch(None)
        assert result == IngestionCardinality(target_cards=None, target_quizzes=None)

    def test_batch_returns_targets(self) -> None:
        result = resolve_from_batch(_batch(cards=5, quizzes=4))
        assert result.target_cards == 5
        assert result.target_quizzes == 4

    def test_card_bounds_use_target_when_set(self) -> None:
        cardinality = IngestionCardinality(target_cards=5, target_quizzes=None)
        assert cardinality.card_bounds() == (5, 5)

    def test_quiz_bounds_use_target_when_set(self) -> None:
        cardinality = IngestionCardinality(target_cards=None, target_quizzes=4)
        assert cardinality.quiz_bounds() == (4, 4)


class TestSourceDocumentIdsFromProvenance:
    def test_collects_unique_ids_in_order(self) -> None:
        d1 = uuid.uuid4()
        d2 = uuid.uuid4()
        provenance = [
            {"source_document_id": str(d1), "content_block_ids": []},
            {"source_document_id": str(d2), "content_block_ids": []},
            {"source_document_id": str(d1), "content_block_ids": []},
        ]
        assert source_document_ids_from_provenance(provenance) == [d1, d2]
