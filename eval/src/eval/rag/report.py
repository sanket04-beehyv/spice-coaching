"""Batch report generation for retrieval evaluation."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from eval.rag.bm25 import CardHit, Hit
from eval.rag.corpus import CardCorpusDoc
from eval.rag.dataset import QuestionLang, lookup_expected_card_index
from eval.rag.embedding import EmbeddingHit
from eval.rag.metrics import card_retrieval_metrics_by_id, retrieval_metrics

_REPORT_TITLES = {
    "bm25": "BM25 Retrieval Report",
    "embedding": "Embedding Retrieval Report",
    "rag": "RAG Chatbot E2E Report",
}

_METRIC_KEYS = ("hit_at_k", "mrr", "precision_at_k", "recall_at_k", "ndcg_at_k")


@dataclass(frozen=True)
class RecordArtifact:
    id: str
    category: str
    question: str
    expected_module: str | None
    relevant_module_ids: list[str]
    is_answerable: bool
    retrieved_module_ids: list[str]
    retrieval_scores: list[float]
    retrieval_metrics: dict[str, float]
    question_lang: QuestionLang | None = None
    expected_card_ids: list[str] = field(default_factory=list)
    expected_card_index: int | None = None
    pipeline_module_id: str | None = None
    retrieved_card_ids: list[str] = field(default_factory=list)
    retrieved_card_titles: list[str] = field(default_factory=list)
    card_retrieval_scores: list[float] = field(default_factory=list)
    card_retrieval_metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BatchReport:
    run_id: str
    retrieval_method: str
    dataset_path: str
    k: int
    corpus_published_count: int
    corpus_embedded_count: int | None
    record_count: int
    evaluated_record_count: int
    skipped_unanswerable_count: int
    aggregate_retrieval_metrics: dict[str, float]
    artifacts: list[RecordArtifact]
    generated_at: str
    aggregate_card_retrieval_metrics: dict[str, float]
    skipped_unresolvable_count: int = 0


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _aggregate_metric_dicts(
    artifacts: list[RecordArtifact],
    attr: str,
) -> dict[str, float]:
    aggregates: dict[str, float] = {}
    for key in _METRIC_KEYS:
        aggregates[key] = _mean([getattr(artifact, attr)[key] for artifact in artifacts])
    return aggregates


def _card_display_title(hit: CardHit) -> str:
    return hit.primary_title or hit.title_en or hit.title_bn or ""


def build_batch_report(
    *,
    run_id: str,
    retrieval_method: str,
    dataset_path: Path,
    k: int,
    corpus_published_count: int,
    corpus_embedded_count: int | None,
    artifacts: list[RecordArtifact],
    skipped_unanswerable_count: int,
    skipped_unresolvable_count: int = 0,
) -> BatchReport:
    card_artifacts = [artifact for artifact in artifacts if artifact.card_retrieval_metrics]
    return BatchReport(
        run_id=run_id,
        retrieval_method=retrieval_method,
        dataset_path=str(dataset_path),
        k=k,
        corpus_published_count=corpus_published_count,
        corpus_embedded_count=corpus_embedded_count,
        record_count=len(artifacts) + skipped_unanswerable_count + skipped_unresolvable_count,
        evaluated_record_count=len(artifacts),
        skipped_unanswerable_count=skipped_unanswerable_count,
        skipped_unresolvable_count=skipped_unresolvable_count,
        aggregate_retrieval_metrics=_aggregate_metric_dicts(artifacts, "retrieval_metrics"),
        aggregate_card_retrieval_metrics=_aggregate_metric_dicts(card_artifacts, "card_retrieval_metrics"),
        artifacts=artifacts,
        generated_at=datetime.now(UTC).isoformat(),
    )


def artifact_from_bm25_hits(
    *,
    record_id: str,
    category: str,
    question: str,
    expected_module: str | None,
    is_answerable: bool,
    relevant_module_ids: list[UUID],
    hits: list[Hit],
    k: int,
) -> RecordArtifact:
    retrieved_module_ids = [hit.module_id for hit in hits]
    metrics = retrieval_metrics(relevant_module_ids, retrieved_module_ids, k=k)
    return RecordArtifact(
        id=record_id,
        category=category,
        question=question,
        expected_module=expected_module,
        relevant_module_ids=[str(module_id) for module_id in relevant_module_ids],
        is_answerable=is_answerable,
        retrieved_module_ids=[str(module_id) for module_id in retrieved_module_ids],
        retrieval_scores=[hit.bm25_score for hit in hits],
        retrieval_metrics=metrics,
    )


def artifact_from_pipeline_hits(
    *,
    record_id: str,
    category: str,
    question: str,
    is_answerable: bool,
    relevant_module_ids: list[UUID],
    expected_card_ids: tuple[UUID, ...],
    expected_card_index: int | None,
    question_lang: QuestionLang,
    module_hits: list[Hit],
    card_hits: list[CardHit],
    k: int,
) -> RecordArtifact:
    retrieved_module_ids = [hit.module_id for hit in module_hits]
    module_metrics = retrieval_metrics(relevant_module_ids, retrieved_module_ids, k=k)
    pipeline_module_id = str(module_hits[0].module_id) if module_hits else None
    retrieved_card_ids = [str(hit.card_id) for hit in card_hits]
    retrieved_card_titles = [_card_display_title(hit) for hit in card_hits]
    card_metrics = card_retrieval_metrics_by_id(
        set(expected_card_ids),
        [hit.card_id for hit in card_hits],
        k=k,
    )
    return RecordArtifact(
        id=record_id,
        category=category,
        question=question,
        expected_module=None,
        relevant_module_ids=[str(module_id) for module_id in relevant_module_ids],
        is_answerable=is_answerable,
        retrieved_module_ids=[str(module_id) for module_id in retrieved_module_ids],
        retrieval_scores=[hit.bm25_score for hit in module_hits],
        retrieval_metrics=module_metrics,
        question_lang=question_lang,
        expected_card_ids=[str(card_id) for card_id in expected_card_ids],
        expected_card_index=expected_card_index,
        pipeline_module_id=pipeline_module_id,
        retrieved_card_ids=retrieved_card_ids,
        retrieved_card_titles=retrieved_card_titles,
        card_retrieval_scores=[hit.bm25_score for hit in card_hits],
        card_retrieval_metrics=card_metrics,
    )


def artifact_from_pipeline_hits_with_lookup(
    *,
    record_id: str,
    category: str,
    question: str,
    is_answerable: bool,
    relevant_module_ids: list[UUID],
    expected_module_id: UUID,
    expected_card_ids: tuple[UUID, ...],
    question_lang: QuestionLang,
    module_hits: list[Hit],
    card_hits: list[CardHit],
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
    k: int,
) -> RecordArtifact:
    lookup_card_id = expected_card_ids[0] if expected_card_ids else None
    expected_card_index = (
        lookup_expected_card_index(
            card_id=lookup_card_id,
            module_ids=[expected_module_id],
            cards_by_module=cards_by_module,
        )
        if lookup_card_id is not None
        else None
    )
    return artifact_from_pipeline_hits(
        record_id=record_id,
        category=category,
        question=question,
        is_answerable=is_answerable,
        relevant_module_ids=relevant_module_ids,
        expected_card_ids=expected_card_ids,
        expected_card_index=expected_card_index,
        question_lang=question_lang,
        module_hits=module_hits,
        card_hits=card_hits,
        k=k,
    )


def artifact_from_embedding_hits(
    *,
    record_id: str,
    category: str,
    question: str,
    expected_module: str | None,
    is_answerable: bool,
    relevant_module_ids: list[UUID],
    hits: list[EmbeddingHit],
    k: int,
) -> RecordArtifact:
    retrieved_module_ids = [hit.module_id for hit in hits]
    metrics = retrieval_metrics(relevant_module_ids, retrieved_module_ids, k=k)
    return RecordArtifact(
        id=record_id,
        category=category,
        question=question,
        expected_module=expected_module,
        relevant_module_ids=[str(module_id) for module_id in relevant_module_ids],
        is_answerable=is_answerable,
        retrieved_module_ids=[str(module_id) for module_id in retrieved_module_ids],
        retrieval_scores=[hit.cosine_distance for hit in hits],
        retrieval_metrics=metrics,
    )


def write_batch_report(report: BatchReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    title = _REPORT_TITLES.get(report.retrieval_method, "Retrieval Report")
    md_path = output_path.with_suffix(".md")
    if report.corpus_embedded_count is not None:
        corpus_line = (
            f"Corpus       : published={report.corpus_published_count}, "
            f"embedded={report.corpus_embedded_count}"
        )
    else:
        corpus_line = f"Corpus       : published={report.corpus_published_count}"
    lines = [
        title,
        "=" * len(title),
        f"Run ID       : {report.run_id}",
        f"Dataset      : {report.dataset_path}",
        f"K            : {report.k}",
        corpus_line,
        f"Evaluated    : {report.evaluated_record_count} "
        f"(skipped unanswerable: {report.skipped_unanswerable_count}, "
        f"skipped unresolvable: {report.skipped_unresolvable_count})",
        "",
        "AGGREGATE MODULE RETRIEVAL",
        "─────────────────────────────────────────────",
    ]
    for key, value in report.aggregate_retrieval_metrics.items():
        label = key.replace("_", " ").title()
        lines.append(f"{label:<22}: {value:6.3f}")
    if report.aggregate_card_retrieval_metrics:
        lines.extend(
            [
                "",
                "AGGREGATE CARD RETRIEVAL (pipeline, top-1 module)",
                "─────────────────────────────────────────────",
            ]
        )
        for key, value in report.aggregate_card_retrieval_metrics.items():
            label = key.replace("_", " ").title()
            lines.append(f"{label:<22}: {value:6.3f}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class RagRecordArtifact:
    id: str
    category: str
    category_slug: str
    language: str
    query: str
    expected_answer: str
    expected_module_ids: list[str]
    expected_card_ids: list[str]
    answerable: str
    is_out_of_scope: bool
    answer: str
    model: str | None
    retrieved_module_ids: list[str]
    cited_module_ids: list[str]
    suggested_questions: list[str]
    cosine_distances: list[float]
    latency_ms: dict[str, float | None]
    token_usage: dict[str, int]
    error: str | None
    e2e_metrics: dict[str, float | bool | None]
    retrieval_metrics: dict[str, float | int | bool | None]
    context_metrics: dict[str, float]
    citation_metrics: dict[str, float | bool | None]
    judge_metrics: dict[str, float | str | None]


@dataclass(frozen=True)
class RagBatchReport:
    run_id: str
    dataset_path: str
    k: int
    corpus_published_count: int
    corpus_embedded_count: int
    record_count: int
    evaluated_record_count: int
    e2e_summary: dict[str, object]
    retrieval_summary: dict[str, float]
    context_summary: dict[str, float]
    citation_summary: dict[str, float]
    judge_summary: dict[str, float]
    error_summary: dict[str, float]
    perf_summary: dict[str, float]
    artifacts: list[RagRecordArtifact]
    generated_at: str


def rag_record_artifact_from_result(result_dict: dict[str, object]) -> RagRecordArtifact:
    return RagRecordArtifact(
        id=str(result_dict["id"]),
        category=str(result_dict["category"]),
        category_slug=str(result_dict["category_slug"]),
        language=str(result_dict["language"]),
        query=str(result_dict["query"]),
        expected_answer=str(result_dict["expected_answer"]),
        expected_module_ids=[str(module_id) for module_id in result_dict["expected_module_ids"]],  # type: ignore[union-attr]
        expected_card_ids=[str(card_id) for card_id in result_dict.get("expected_card_ids", [])],  # type: ignore[union-attr]
        answerable=str(result_dict.get("answerable", "yes")),
        is_out_of_scope=bool(result_dict["is_out_of_scope"]),
        answer=str(result_dict["answer"]),
        model=result_dict.get("model") if result_dict.get("model") is None else str(result_dict["model"]),
        retrieved_module_ids=[str(module_id) for module_id in result_dict["retrieved_module_ids"]],  # type: ignore[union-attr]
        cited_module_ids=[str(module_id) for module_id in result_dict["cited_module_ids"]],  # type: ignore[union-attr]
        suggested_questions=[str(q) for q in result_dict.get("suggested_questions", [])],  # type: ignore[union-attr]
        cosine_distances=[float(value) for value in result_dict["cosine_distances"]],  # type: ignore[union-attr]
        latency_ms=dict(result_dict["latency_ms"]),  # type: ignore[arg-type]
        token_usage=dict(result_dict["token_usage"]),  # type: ignore[arg-type]
        error=result_dict.get("error") if result_dict.get("error") is None else str(result_dict["error"]),
        e2e_metrics=dict(result_dict["e2e_metrics"]),  # type: ignore[arg-type]
        retrieval_metrics=dict(result_dict.get("retrieval_metrics") or {}),  # type: ignore[arg-type]
        context_metrics=dict(result_dict.get("context_metrics") or {}),  # type: ignore[arg-type]
        citation_metrics=dict(result_dict.get("citation_metrics") or {}),  # type: ignore[arg-type]
        judge_metrics=dict(result_dict.get("judge_metrics") or {}),  # type: ignore[arg-type]
    )


def build_rag_batch_report(
    *,
    run_id: str,
    dataset_path: Path,
    k: int,
    corpus_published_count: int,
    corpus_embedded_count: int,
    artifact_dicts: list[dict[str, object]],
    e2e_summary: dict[str, object],
) -> RagBatchReport:
    retrieval_summary = dict(e2e_summary.get("retrieval_summary") or {})  # type: ignore[arg-type]
    context_summary = dict(e2e_summary.get("context_summary") or {})  # type: ignore[arg-type]
    citation_summary = dict(e2e_summary.get("citation_summary") or {})  # type: ignore[arg-type]
    judge_summary = dict(e2e_summary.get("judge_summary") or {})  # type: ignore[arg-type]
    error_summary = dict(e2e_summary.get("error_summary") or {})  # type: ignore[arg-type]
    perf_summary = dict(e2e_summary.get("perf_summary") or {})  # type: ignore[arg-type]
    artifacts = [rag_record_artifact_from_result(item) for item in artifact_dicts]
    return RagBatchReport(
        run_id=run_id,
        dataset_path=str(dataset_path),
        k=k,
        corpus_published_count=corpus_published_count,
        corpus_embedded_count=corpus_embedded_count,
        record_count=len(artifacts),
        evaluated_record_count=len(artifacts),
        e2e_summary=e2e_summary,
        retrieval_summary=retrieval_summary,
        context_summary=context_summary,
        citation_summary=citation_summary,
        judge_summary=judge_summary,
        error_summary=error_summary,
        perf_summary=perf_summary,
        artifacts=artifacts,
        generated_at=datetime.now(UTC).isoformat(),
    )


def _append_metric_section(
    lines: list[str],
    title: str,
    metrics: dict[str, float],
) -> None:
    if not metrics:
        return
    lines.extend(["", title, "─────────────────────────────────────────────"])
    for key, value in metrics.items():
        label = key.replace("_", " ").title()
        lines.append(f"{label:<22}: {value:6.3f}")


def write_rag_batch_report(report: RagBatchReport, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    title = _REPORT_TITLES["rag"]
    md_path = output_path.with_suffix(".md")
    e2e = report.e2e_summary
    lines = [
        title,
        "=" * len(title),
        f"Run ID       : {report.run_id}",
        f"Dataset      : {report.dataset_path}",
        f"K            : {report.k}",
        (
            f"Corpus       : published={report.corpus_published_count}, "
            f"embedded={report.corpus_embedded_count}"
        ),
        f"Evaluated    : {report.evaluated_record_count}",
        "",
        "RETRIEVAL",
        "─────────────────────────────────────────────",
    ]
    for key in _METRIC_KEYS:
        value = report.retrieval_summary.get(key, 0.0)
        label = key.replace("_", " ").title()
        lines.append(f"{label:<22}: {value:6.3f}")
    if report.retrieval_summary.get("retrieval_miss_rate") is not None:
        lines.append(
            f"{'Retrieval Miss Rate':<22}: {report.retrieval_summary.get('retrieval_miss_rate', 0.0):6.3f}"
        )

    _append_metric_section(lines, "CONTEXT (PROXY)", report.context_summary)
    _append_metric_section(lines, "CITATION", report.citation_summary)

    lines.extend(
        [
            "",
            "ANSWER",
            "─────────────────────────────────────────────",
            f"{'Token F1 (avg)':<22}: {float(e2e.get('avg_token_f1', 0.0)):6.3f}",
            f"{'Token Recall (avg)':<22}: {float(e2e.get('avg_token_recall', 0.0)):6.3f}",
            f"{'Exact Match (avg)':<22}: {float(e2e.get('avg_exact_match', 0.0)):6.3f}",
            f"{'Grounding Overlap':<22}: {float(e2e.get('avg_answer_grounding_overlap', 0.0)):6.3f}",
            f"{'Partial Correct Rate':<22}: {float(e2e.get('partial_answer_correct_rate', 0.0)):6.3f}",
            f"{'Abstention Rate':<22}: {float(e2e.get('abstention_rate', 0.0)):6.3f}",
            f"{'False Refusal Rate':<22}: {float(e2e.get('false_refusal_rate', 0.0)):6.3f}",
            f"{'Citation Accuracy':<22}: {float(e2e.get('avg_citation_accuracy', 0.0)):6.3f}",
            f"{'Safety Pass Rate':<22}: {float(e2e.get('safety_pass_rate', 0.0)):6.3f}",
        ]
    )

    _append_metric_section(lines, "LLM JUDGE", report.judge_summary)
    _append_metric_section(lines, "ERRORS", report.error_summary)

    lines.extend(
        [
            "",
            "PERFORMANCE",
            "─────────────────────────────────────────────",
            f"{'P50 Latency (E2E)':<22}: {report.perf_summary.get('total_p50', 0.0):.0f}ms",
            f"{'P90 Latency (E2E)':<22}: {report.perf_summary.get('total_p90', 0.0):.0f}ms",
            f"{'P95 Latency (E2E)':<22}: {report.perf_summary.get('total_p95', 0.0):.0f}ms",
            f"{'P50 Embed Latency':<22}: {report.perf_summary.get('embed_p50', 0.0):.0f}ms",
            (
                f"{'Cost (avg tokens)':<22}: "
                f"in={report.perf_summary.get('avg_input_tokens', 0.0):.0f} "
                f"out={report.perf_summary.get('avg_output_tokens', 0.0):.0f}"
            ),
        ]
    )

    by_category = e2e.get("by_category")
    if isinstance(by_category, dict) and by_category:
        lines.extend(
            [
                "",
                "PER-CATEGORY",
                "─────────────────────────────────────────────",
            ]
        )
        for category, metrics in sorted(by_category.items()):
            if not isinstance(metrics, dict):
                continue
            parts = [f"{category:<22}:"]
            for metric_name in ("token_f1", "hit_at_k", "strict_citation_accuracy", "faithfulness"):
                if metric_name in metrics:
                    parts.append(f" {metric_name}={float(metrics[metric_name]):.3f}")
            lines.append("".join(parts))

    by_answerable = e2e.get("by_answerable")
    if isinstance(by_answerable, dict) and by_answerable:
        lines.extend(
            [
                "",
                "BY ANSWERABLE",
                "─────────────────────────────────────────────",
            ]
        )
        for answerable, metrics in sorted(by_answerable.items()):
            if not isinstance(metrics, dict):
                continue
            lines.append(
                f"{answerable:<22}: count={int(metrics.get('count', 0))} "
                f"token_f1={float(metrics.get('avg_token_f1', 0.0)):.3f} "
                f"abstention={float(metrics.get('abstention_correct_rate', 0.0)):.3f}"
            )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
