"""Retrieval evaluation metrics."""

from __future__ import annotations

import math
from uuid import UUID


def hit_at_k(relevant: set[UUID], retrieved: list[UUID], k: int) -> float:
    if not relevant:
        return 0.0
    top_k = set(retrieved[:k])
    return 1.0 if relevant & top_k else 0.0


def mrr(relevant: set[UUID], retrieved: list[UUID]) -> float:
    if not relevant:
        return 0.0
    for rank, module_id in enumerate(retrieved, start=1):
        if module_id in relevant:
            return 1.0 / rank
    return 0.0


def precision_at_k(relevant: set[UUID], retrieved: list[UUID], k: int) -> float:
    if k <= 0:
        return 0.0
    top_k = retrieved[:k]
    if not top_k:
        return 0.0
    return len(relevant & set(top_k)) / k


def recall_at_k(relevant: set[UUID], retrieved: list[UUID], k: int) -> float:
    if not relevant:
        return 0.0
    top_k = set(retrieved[:k])
    return len(relevant & top_k) / len(relevant)


def ndcg_at_k(relevant: set[UUID], retrieved: list[UUID], k: int) -> float:
    if not relevant:
        return 0.0
    top_k = retrieved[:k]
    dcg = 0.0
    for rank, module_id in enumerate(top_k, start=1):
        if module_id in relevant:
            dcg += 1.0 / math.log2(rank + 1)
    ideal_hits = min(len(relevant), k)
    if ideal_hits == 0:
        return 0.0
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def retrieval_metrics(
    relevant_module_ids: list[UUID],
    retrieved_module_ids: list[UUID],
    *,
    k: int,
) -> dict[str, float]:
    relevant = set(relevant_module_ids)
    return {
        "hit_at_k": hit_at_k(relevant, retrieved_module_ids, k),
        "mrr": mrr(relevant, retrieved_module_ids),
        "precision_at_k": precision_at_k(relevant, retrieved_module_ids, k),
        "recall_at_k": recall_at_k(relevant, retrieved_module_ids, k),
        "ndcg_at_k": ndcg_at_k(relevant, retrieved_module_ids, k),
    }


def retrieval_diagnostics(
    relevant_module_ids: list[UUID],
    retrieved_module_ids: list[UUID],
    cosine_distances: list[float],
) -> dict[str, float | int | bool | None]:
    """Per-record retrieval diagnostics beyond aggregate IR metrics."""
    relevant = set(relevant_module_ids)
    gold_rank: int | None = None
    gold_cosine_distance: float | None = None
    for rank, module_id in enumerate(retrieved_module_ids, start=1):
        if module_id in relevant:
            gold_rank = rank
            idx = rank - 1
            if idx < len(cosine_distances):
                gold_cosine_distance = cosine_distances[idx]
            break
    return {
        "gold_rank": gold_rank,
        "gold_cosine_distance": gold_cosine_distance,
        "retrieval_miss": gold_rank is None,
    }


def _hit_at_k_str(relevant: set[str], retrieved: list[str], k: int) -> float:
    if not relevant:
        return 0.0
    top_k = set(retrieved[:k])
    return 1.0 if relevant & top_k else 0.0


def _mrr_str(relevant: set[str], retrieved: list[str]) -> float:
    if not relevant:
        return 0.0
    for rank, title in enumerate(retrieved, start=1):
        if title in relevant:
            return 1.0 / rank
    return 0.0


def _precision_at_k_str(relevant: set[str], retrieved: list[str], k: int) -> float:
    if k <= 0:
        return 0.0
    top_k = retrieved[:k]
    if not top_k:
        return 0.0
    return len(relevant & set(top_k)) / k


def _recall_at_k_str(relevant: set[str], retrieved: list[str], k: int) -> float:
    if not relevant:
        return 0.0
    top_k = set(retrieved[:k])
    return len(relevant & top_k) / len(relevant)


def _ndcg_at_k_str(relevant: set[str], retrieved: list[str], k: int) -> float:
    if not relevant:
        return 0.0
    top_k = retrieved[:k]
    dcg = 0.0
    for rank, title in enumerate(top_k, start=1):
        if title in relevant:
            dcg += 1.0 / math.log2(rank + 1)
    ideal_hits = min(len(relevant), k)
    if ideal_hits == 0:
        return 0.0
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def card_retrieval_metrics_by_id(
    expected_card_ids: set[UUID],
    retrieved_card_ids: list[UUID],
    *,
    k: int,
) -> dict[str, float]:
    relevant = {str(card_id) for card_id in expected_card_ids}
    retrieved = [str(card_id) for card_id in retrieved_card_ids]
    return {
        "hit_at_k": _hit_at_k_str(relevant, retrieved, k),
        "mrr": _mrr_str(relevant, retrieved),
        "precision_at_k": _precision_at_k_str(relevant, retrieved, k),
        "recall_at_k": _recall_at_k_str(relevant, retrieved, k),
        "ndcg_at_k": _ndcg_at_k_str(relevant, retrieved, k),
    }
