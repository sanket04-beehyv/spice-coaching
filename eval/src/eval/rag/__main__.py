"""CLI entry point for RAG retrieval and chatbot evaluation."""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from platform_service.integrations.ai_runtime_client import AIRuntimeClient

from eval.rag.answer_metrics import aggregate_e2e_summaries
from eval.rag.bm25 import Bm25Index, build_card_indexes
from eval.rag.corpus import (
    build_module_card_corpus,
    corpus_docs_from_modules,
    count_embedded_published_modules,
    load_cards_by_module_ids,
    load_published_corpus,
    load_published_modules,
)
from eval.rag.dataset import (
    collect_golden_resolution_issues,
    load_golden_dataset,
    resolve_golden_labels,
    unresolvable_golden_record_ids,
)
from eval.rag.embedding import EmbeddingRetriever
from eval.rag.llm_judge import LlmJudge
from eval.rag.rag_dataset import (
    load_rag_golden_dataset,
    validate_expected_card_ids,
    validate_expected_module_ids,
)
from eval.rag.rag_runner import RagQueryRunner, rag_result_to_artifact_dict
from eval.rag.report import (
    artifact_from_bm25_hits,
    artifact_from_embedding_hits,
    artifact_from_pipeline_hits_with_lookup,
    build_batch_report,
    build_rag_batch_report,
    write_batch_report,
    write_rag_batch_report,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate retrieval over published modules in the database.",
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Search query (omit to run batch eval against --dataset)",
    )
    parser.add_argument(
        "--method",
        choices=["bm25", "embedding", "rag"],
        default="bm25",
        help="Evaluation method: bm25, embedding, or rag (default: bm25)",
    )
    parser.add_argument("--k", type=int, default=5, help="Top-K results (default: 5)")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("eval/rag/golden/golden_dataset.json"),
        help="Golden JSON dataset for batch retrieval eval (default: eval/rag/golden/golden_dataset.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Batch JSON report path (default: eval/rag/reports/<method>-run.json)",
    )
    parser.add_argument("--tenant-id", type=UUID, default=None, help="Optional tenant UUID filter")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Batch run identifier (default: <method>-YYYYMMDD-HHMMSS)",
    )
    parser.add_argument(
        "--ai-runtime-url",
        default="http://localhost:8000/",
        help="ai-runtime base URL override (default: platform AI_RUNTIME_BASE_URL)",
    )
    parser.add_argument(
        "--ai-runtime-token",
        default="dev-internal-token",
        help="ai-runtime internal token override (default: platform AI_RUNTIME_TOKEN)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="RAG batch only: evaluate at most N records (default: all)",
    )
    parser.add_argument(
        "--record-id",
        default=None,
        help="RAG batch only: evaluate a single record id",
    )
    parser.add_argument(
        "--llm-judge",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="RAG batch only: run LLM-as-judge scoring (default: on)",
    )
    return parser.parse_args(argv)


def _apply_method_defaults(args: argparse.Namespace) -> None:
    if args.output is None:
        args.output = Path(f"eval/rag/reports/{args.method}-run.json")


def _print_bm25_hits(*, query: str, corpus_count: int, hits: list) -> None:
    print(f"Corpus: {corpus_count} published modules")
    print(f'Query: "{query}"')
    print()
    print(f"{'rank':>4}  {'module_id':<36}  {'bm25_score':>10}  title_en")
    for hit in hits:
        title = hit.primary_title or hit.title_en or hit.title_bn or ""
        print(f"{hit.rank:>4}  {str(hit.module_id):<36}  {hit.bm25_score:>10.4f}  {title}")


def _print_embedding_hits(*, query: str, embedded_count: int, hits: list) -> None:
    print(f"Corpus: {embedded_count} embedded published modules")
    print(f'Query: "{query}"')
    print()
    print(f"{'rank':>4}  {'module_id':<36}  {'cosine_dist':>10}  title_en")
    for hit in hits:
        title = hit.primary_title or hit.title_en or hit.title_bn or ""
        print(f"{hit.rank:>4}  {str(hit.module_id):<36}  {hit.cosine_distance:>10.4f}  {title}")


def _record_has_card_eval(record) -> bool:
    if record.expected_module_id is None:
        return False
    return bool(record.expected_card_ids)


async def _run_bm25_single_query(args: argparse.Namespace) -> int:
    if not args.query:
        print("error: query must not be empty", file=sys.stderr)
        return 2

    docs = await load_published_corpus(tenant_id=args.tenant_id)
    index = Bm25Index(docs)
    hits = index.search(args.query, k=args.k)
    _print_bm25_hits(query=args.query, corpus_count=index.doc_count, hits=hits)
    return 0


async def _run_embedding_single_query(args: argparse.Namespace) -> int:
    if not args.query:
        print("error: query must not be empty", file=sys.stderr)
        return 2

    retriever = EmbeddingRetriever(
        tenant_id=args.tenant_id,
        base_url=args.ai_runtime_url,
        token=args.ai_runtime_token,
    )
    try:
        embedded_count = await retriever.embedded_count()
        if embedded_count == 0:
            print(
                "warning: no published modules have embeddings; run the embedding worker first",
                file=sys.stderr,
            )
        hits = await retriever.search(args.query, k=args.k)
        _print_embedding_hits(query=args.query, embedded_count=embedded_count, hits=hits)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await retriever.aclose()
    return 0


async def _run_bm25_batch(args: argparse.Namespace) -> int:
    dataset_path = args.dataset

    try:
        records = load_golden_dataset(dataset_path)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Loaded {len(records)} records from {dataset_path}")

    modules = await load_published_modules(tenant_id=args.tenant_id)
    cards_by_module_raw = await load_cards_by_module_ids([module.id for module in modules])
    docs = corpus_docs_from_modules(modules, cards_by_module_raw)
    records, _label_warnings = resolve_golden_labels(records, docs)

    cards_by_module = build_module_card_corpus(modules, cards_by_module_raw)
    resolution_issues = collect_golden_resolution_issues(records, docs, cards_by_module)
    skip_ids = unresolvable_golden_record_ids(resolution_issues)
    for issue in resolution_issues:
        print(f"warning: {issue.message}", file=sys.stderr)

    index = Bm25Index(docs)
    card_indexes = build_card_indexes(cards_by_module)
    use_pipeline = any(_record_has_card_eval(record) for record in records)

    artifacts = []
    skipped_unanswerable = 0
    skipped_unresolvable = 0
    for record in records:
        if not record.is_answerable:
            skipped_unanswerable += 1
            continue
        if record.id in skip_ids:
            skipped_unresolvable += 1
            continue
        module_hits = index.search(record.question, k=args.k)
        if use_pipeline and _record_has_card_eval(record):
            assert record.expected_module_id is not None
            assert record.expected_card_ids
            assert record.question_lang is not None
            pipeline_module_id = module_hits[0].module_id if module_hits else None
            card_hits = []
            if pipeline_module_id is not None:
                card_index = card_indexes.get(pipeline_module_id)
                if card_index is not None:
                    card_hits = card_index.search(record.question, k=args.k)
            artifacts.append(
                artifact_from_pipeline_hits_with_lookup(
                    record_id=record.id,
                    category=record.category,
                    question=record.question,
                    is_answerable=record.is_answerable,
                    relevant_module_ids=record.relevant_module_ids,
                    expected_module_id=record.expected_module_id,
                    expected_card_ids=record.expected_card_ids,
                    question_lang=record.question_lang,
                    module_hits=module_hits,
                    card_hits=card_hits,
                    cards_by_module=cards_by_module,
                    k=args.k,
                )
            )
        else:
            artifacts.append(
                artifact_from_bm25_hits(
                    record_id=record.id,
                    category=record.category,
                    question=record.question,
                    expected_module=record.expected_module,
                    is_answerable=record.is_answerable,
                    relevant_module_ids=record.relevant_module_ids,
                    hits=module_hits,
                    k=args.k,
                )
            )

    run_id = args.run_id or datetime.now(UTC).strftime("bm25-%Y%m%d-%H%M%S")
    report = build_batch_report(
        run_id=run_id,
        retrieval_method="bm25",
        dataset_path=dataset_path,
        k=args.k,
        corpus_published_count=index.doc_count,
        corpus_embedded_count=None,
        artifacts=artifacts,
        skipped_unanswerable_count=skipped_unanswerable,
        skipped_unresolvable_count=skipped_unresolvable,
    )
    write_batch_report(report, args.output)
    print(f"Wrote {args.output}")
    print(f"Wrote {args.output.with_suffix('.md')}")
    print(f"Evaluated {report.evaluated_record_count} records; corpus={report.corpus_published_count}")
    print("Module metrics:")
    for key, value in report.aggregate_retrieval_metrics.items():
        print(f"  {key}: {value:.3f}")
    if report.aggregate_card_retrieval_metrics:
        print("Card metrics (pipeline):")
        for key, value in report.aggregate_card_retrieval_metrics.items():
            print(f"  {key}: {value:.3f}")
    return 0


async def _run_embedding_batch(args: argparse.Namespace) -> int:
    dataset_path = args.dataset

    try:
        records = load_golden_dataset(dataset_path)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    docs = await load_published_corpus(tenant_id=args.tenant_id)
    records, label_warnings = resolve_golden_labels(records, docs)
    for warning in label_warnings:
        print(f"warning: {warning}", file=sys.stderr)

    retriever = EmbeddingRetriever(
        tenant_id=args.tenant_id,
        base_url=args.ai_runtime_url,
        token=args.ai_runtime_token,
    )
    try:
        embedded_count = await retriever.embedded_count()
        if embedded_count == 0:
            print(
                "warning: no published modules have embeddings; run the embedding worker first",
                file=sys.stderr,
            )

        artifacts = []
        skipped_unanswerable = 0
        for record in records:
            if not record.is_answerable:
                skipped_unanswerable += 1
                continue
            try:
                hits = await retriever.search(record.question, k=args.k)
            except RuntimeError as exc:
                print(f"error: {record.id}: {exc}", file=sys.stderr)
                return 1
            artifacts.append(
                artifact_from_embedding_hits(
                    record_id=record.id,
                    category=record.category,
                    question=record.question,
                    expected_module=record.expected_module,
                    is_answerable=record.is_answerable,
                    relevant_module_ids=record.relevant_module_ids,
                    hits=hits,
                    k=args.k,
                )
            )

        run_id = args.run_id or datetime.now(UTC).strftime("embedding-%Y%m%d-%H%M%S")
        report = build_batch_report(
            run_id=run_id,
            retrieval_method="embedding",
            dataset_path=dataset_path,
            k=args.k,
            corpus_published_count=len(docs),
            corpus_embedded_count=embedded_count,
            artifacts=artifacts,
            skipped_unanswerable_count=skipped_unanswerable,
        )
        write_batch_report(report, args.output)
        print(f"Wrote {args.output}")
        print(f"Wrote {args.output.with_suffix('.md')}")
        print(
            f"Evaluated {report.evaluated_record_count} records; "
            f"corpus published={report.corpus_published_count} embedded={embedded_count}"
        )
        for key, value in report.aggregate_retrieval_metrics.items():
            print(f"  {key}: {value:.3f}")
    finally:
        await retriever.aclose()
    return 0


async def _run_rag_batch(args: argparse.Namespace) -> int:
    if args.query is not None:
        print("error: rag method does not support a positional query; use batch mode", file=sys.stderr)
        return 2

    dataset_path = args.dataset
    try:
        records = load_rag_golden_dataset(dataset_path)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.record_id is not None:
        records = [record for record in records if record.id == args.record_id]
        if not records:
            print(f"error: record id {args.record_id!r} not found in dataset", file=sys.stderr)
            return 1
    elif args.limit is not None:
        if args.limit <= 0:
            print("error: --limit must be positive", file=sys.stderr)
            return 2
        records = records[: args.limit]

    docs = await load_published_corpus(tenant_id=args.tenant_id)
    published_module_ids = {doc.module_id for doc in docs}
    module_warnings = validate_expected_module_ids(records, published_module_ids)
    for warning in module_warnings:
        print(f"warning: {warning}", file=sys.stderr)

    modules = await load_published_modules(tenant_id=args.tenant_id)
    cards_by_module_raw = await load_cards_by_module_ids([module.id for module in modules])
    cards_by_module = build_module_card_corpus(modules, cards_by_module_raw)
    card_warnings = validate_expected_card_ids(records, cards_by_module)
    for warning in card_warnings:
        print(f"warning: {warning}", file=sys.stderr)

    embedded_count = await count_embedded_published_modules(tenant_id=args.tenant_id)
    if embedded_count == 0:
        print(
            "warning: no published modules have embeddings; run the embedding worker first",
            file=sys.stderr,
        )

    llm_judge: LlmJudge | None = None
    if args.llm_judge:
        llm_judge = LlmJudge(
            AIRuntimeClient(base_url=args.ai_runtime_url, token=args.ai_runtime_token),
        )

    runner = RagQueryRunner(
        tenant_id=args.tenant_id,
        base_url=args.ai_runtime_url,
        token=args.ai_runtime_token,
        cards_by_module=cards_by_module,
        llm_judge=llm_judge,
    )
    artifact_dicts: list[dict[str, object]] = []
    try:
        for index, record in enumerate(records, start=1):
            preview = record.query if len(record.query) <= 80 else f"{record.query[:80]}..."
            print(f"[{index}/{len(records)}] {record.id}: {preview}", file=sys.stderr)
            result = await runner.run_record(record, k=args.k)
            if result.error:
                print(f"warning: {record.id}: {result.error}", file=sys.stderr)
            artifact_dicts.append(rag_result_to_artifact_dict(result))
    finally:
        await runner.aclose()

    e2e_summary = aggregate_e2e_summaries(artifact_dicts)
    run_id = args.run_id or datetime.now(UTC).strftime("rag-%Y%m%d-%H%M%S")
    report = build_rag_batch_report(
        run_id=run_id,
        dataset_path=dataset_path,
        k=args.k,
        corpus_published_count=len(docs),
        corpus_embedded_count=embedded_count,
        artifact_dicts=artifact_dicts,
        e2e_summary=e2e_summary,
    )
    write_rag_batch_report(report, args.output)
    print(f"Wrote {args.output}")
    print(f"Wrote {args.output.with_suffix('.md')}")
    print(
        f"Evaluated {report.evaluated_record_count} records; "
        f"corpus published={report.corpus_published_count} embedded={embedded_count}"
    )
    print("E2E metrics:")
    print(f"  avg_token_f1: {float(e2e_summary['avg_token_f1']):.3f}")
    print(f"  avg_token_recall: {float(e2e_summary.get('avg_token_recall', 0.0)):.3f}")
    print(f"  abstention_rate: {float(e2e_summary['abstention_rate']):.3f}")
    print(f"  false_refusal_rate: {float(e2e_summary['false_refusal_rate']):.3f}")
    context_summary = e2e_summary.get("context_summary")
    if isinstance(context_summary, dict) and context_summary:
        print("Context metrics:")
        for key, value in context_summary.items():
            print(f"  {key}: {float(value):.3f}")
    citation_summary = e2e_summary.get("citation_summary")
    if isinstance(citation_summary, dict) and citation_summary:
        print("Citation metrics:")
        for key, value in citation_summary.items():
            print(f"  {key}: {float(value):.3f}")
    judge_summary = e2e_summary.get("judge_summary")
    if isinstance(judge_summary, dict) and judge_summary:
        print("LLM judge:")
        for key, value in judge_summary.items():
            print(f"  {key}: {float(value):.3f}")
    if report.retrieval_summary:
        print("Retrieval metrics:")
        for key, value in report.retrieval_summary.items():
            print(f"  {key}: {value:.3f}")
    return 0


async def _async_main(args: argparse.Namespace) -> int:
    if args.method == "rag":
        return await _run_rag_batch(args)
    if args.method == "bm25":
        if args.query is None:
            return await _run_bm25_batch(args)
        return await _run_bm25_single_query(args)
    if args.query is None:
        return await _run_embedding_batch(args)
    return await _run_embedding_single_query(args)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _apply_method_defaults(args)
    if args.k <= 0:
        print("error: --k must be positive", file=sys.stderr)
        return 2
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
