"""Citation accuracy metrics for RAG evaluation."""

from __future__ import annotations

from uuid import UUID

from eval.rag.answer_metrics import citation_accuracy, is_refusal
from eval.rag.rag_dataset import RagGoldenRecord


def strict_citation_accuracy(
    *,
    reference_module_ids: list[UUID],
    cited_module_ids: list[UUID],
) -> float | None:
    if not reference_module_ids:
        return None
    reference_set = set(reference_module_ids)
    if cited_module_ids and reference_set & set(cited_module_ids):
        return 1.0
    return 0.0


def citation_precision(
    *,
    reference_module_ids: list[UUID],
    cited_module_ids: list[UUID],
) -> float | None:
    if not cited_module_ids:
        return None
    reference_set = set(reference_module_ids)
    return len(reference_set & set(cited_module_ids)) / len(cited_module_ids)


def citation_recall(
    *,
    reference_module_ids: list[UUID],
    cited_module_ids: list[UUID],
) -> float | None:
    if not reference_module_ids:
        return None
    reference_set = set(reference_module_ids)
    if not cited_module_ids:
        return 0.0
    return len(reference_set & set(cited_module_ids)) / len(reference_set)


def spurious_citation(
    *,
    cited_module_ids: list[UUID],
    retrieved_module_ids: list[UUID],
) -> bool:
    if not cited_module_ids:
        return False
    retrieved_set = set(retrieved_module_ids)
    return any(module_id not in retrieved_set for module_id in cited_module_ids)


def uncited_but_answered(
    *,
    answer: str,
    cited_module_ids: list[UUID],
    is_out_of_scope: bool,
) -> bool:
    if is_out_of_scope:
        return False
    if is_refusal(answer):
        return False
    if not answer.strip():
        return False
    return not cited_module_ids


def compute_citation_metrics(
    *,
    record: RagGoldenRecord,
    answer: str,
    cited_module_ids: list[UUID],
    retrieved_module_ids: list[UUID],
) -> dict[str, float | bool | None]:
    reference = list(record.expected_module_ids)
    return {
        "strict_citation_accuracy": strict_citation_accuracy(
            reference_module_ids=reference,
            cited_module_ids=cited_module_ids,
        ),
        "citation_or_retrieval_accuracy": citation_accuracy(
            reference_module_ids=reference,
            cited_module_ids=cited_module_ids,
            retrieved_module_ids=retrieved_module_ids,
        ),
        "citation_precision": citation_precision(
            reference_module_ids=reference,
            cited_module_ids=cited_module_ids,
        ),
        "citation_recall": citation_recall(
            reference_module_ids=reference,
            cited_module_ids=cited_module_ids,
        ),
        "spurious_citation": spurious_citation(
            cited_module_ids=cited_module_ids,
            retrieved_module_ids=retrieved_module_ids,
        ),
        "uncited_but_answered": uncited_but_answered(
            answer=answer,
            cited_module_ids=cited_module_ids,
            is_out_of_scope=record.is_out_of_scope,
        ),
    }
