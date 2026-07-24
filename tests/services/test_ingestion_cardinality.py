"""Tests for ingest-time card/quiz cardinality resolution."""

from __future__ import annotations

import uuid

from platform_service.db.models.source_document import SourceDocument
from platform_service.services.ingestion_cardinality import (
    IngestionCardinality,
    resolve_from_source_documents,
    source_document_ids_from_provenance,
)


def _doc(
    *,
    cards: int | None = None,
    quizzes: int | None = None,
) -> SourceDocument:
    return SourceDocument(
        title="t",
        source_type="pdf",
        primary_language="en",
        content_domain="clinical",
        assessment_mode="with_quiz",
        original_storage_path="/tmp/x.pdf",
        target_cards_per_module=cards,
        target_quizzes_per_module=quizzes,
    )


class TestResolveFromSourceDocuments:
    def test_empty_documents_returns_none_targets(self) -> None:
        result = resolve_from_source_documents([])
        assert result == IngestionCardinality(target_cards=None, target_quizzes=None)

    def test_single_document_returns_targets(self) -> None:
        result = resolve_from_source_documents([_doc(cards=5, quizzes=4)])
        assert result.target_cards == 5
        assert result.target_quizzes == 4

    def test_agreeing_documents_return_shared_targets(self) -> None:
        result = resolve_from_source_documents(
            [
                _doc(cards=5, quizzes=4),
                _doc(cards=5, quizzes=4),
            ]
        )
        assert result.target_cards == 5
        assert result.target_quizzes == 4

    def test_conflicting_card_targets_fall_back_to_none(self) -> None:
        result = resolve_from_source_documents([_doc(cards=5), _doc(cards=6)])
        assert result.target_cards is None
        assert result.target_quizzes is None

    def test_conflicting_quiz_targets_only_clears_quizzes(self) -> None:
        result = resolve_from_source_documents([_doc(cards=5, quizzes=4), _doc(cards=5, quizzes=6)])
        assert result.target_cards == 5
        assert result.target_quizzes is None

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
