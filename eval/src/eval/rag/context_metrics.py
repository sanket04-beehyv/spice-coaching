"""Context-proxy metrics using retrieved modules and corpus card metadata."""

from __future__ import annotations

from uuid import UUID

from eval.rag.corpus import CardCorpusDoc
from eval.rag.metrics import card_retrieval_metrics_by_id
from eval.rag.rag_dataset import RagGoldenRecord


def card_ids_from_retrieved_modules(
    retrieved_module_ids: list[UUID],
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
) -> list[UUID]:
    card_ids: list[UUID] = []
    for module_id in retrieved_module_ids:
        for card in cards_by_module.get(module_id, []):
            card_ids.append(card.card_id)
    return card_ids


def gold_card_hit(
    expected_card_ids: tuple[UUID, ...],
    retrieved_card_ids: list[UUID],
) -> float:
    if not expected_card_ids:
        return 0.0
    relevant = set(expected_card_ids)
    return 1.0 if relevant & set(retrieved_card_ids) else 0.0


def card_recall_at_k(
    expected_card_ids: tuple[UUID, ...],
    retrieved_card_ids: list[UUID],
    *,
    k: int,
) -> float:
    if not expected_card_ids:
        return 0.0
    relevant = set(expected_card_ids)
    top_k = set(retrieved_card_ids[:k])
    return len(relevant & top_k) / len(relevant)


def compute_context_metrics(
    *,
    record: RagGoldenRecord,
    retrieved_module_ids: list[UUID],
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
    k: int,
) -> dict[str, float] | None:
    if record.is_out_of_scope or not record.expected_card_ids:
        return None

    retrieved_ids = card_ids_from_retrieved_modules(retrieved_module_ids, cards_by_module)
    card_metrics = card_retrieval_metrics_by_id(
        set(record.expected_card_ids),
        retrieved_ids,
        k=k,
    )
    return {
        "gold_card_hit": gold_card_hit(record.expected_card_ids, retrieved_ids),
        "card_recall_at_k": card_recall_at_k(record.expected_card_ids, retrieved_ids, k=k),
        "card_mrr": card_metrics["mrr"],
    }
