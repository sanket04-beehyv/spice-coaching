"""Stage 2 orchestrator — corpus-level module identification via token-budget chunking.

Replaces the prior outline-partitioner + LLM-consolidator architecture (which
amplified outline noise into 60+ overlapping LLM calls and a brittle merge
step). New flow:

1. Build the page_corpus from source_pages + content_blocks
2. Chunk into token-budgeted, content-disjoint slices via `chunk_by_token_budget`
3. Run `module_identifier` once per chunk (in parallel, bounded by
   `stage_c_section_concurrency`)
4. Backfill any empty `content_block_ids` from each chunk's actual blocks
5. Dedup candidates by normalised title across chunks; flag near-duplicates
   for reviewer attention via trigram similarity
6. Run the advisory `insufficient_source_filter` for quality flags
7. Persist as `module_candidate_draft` rows for Stage 2-draft

For the SK manual (~250K tokens, target 60K, window 10%): produces ~4 chunks.
For SOPs under the target: produces 1 chunk (single-call path).

Architecture-reset note: gap context was removed from the prompt. The
behavioural_gap registry is no longer loaded or threaded through the
identifier — gaps are runtime telemetry, not source-document structure.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.localized import candidate_description_localized
from platform_service.services.corpus_partitioner import (
    chunk_by_token_budget,
    dedup_and_flag_cross_chunk,
    estimate_corpus_tokens,
)
from platform_service.services.insufficient_source_filter import (
    FilterDecision,
    evaluate_candidate,
)
from platform_service.services.module_identifier import (
    ModuleIdentifier,
)
from platform_service.workers.extractors.content_block_parser import (
    estimate_token_count,
    parse_page_blocks,
)

logger = logging.getLogger(__name__)


def _backfill_empty_block_ids(
    candidates: list[dict[str, Any]],
    chunk_page_corpus: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Repair candidates whose `content_block_ids` were emitted empty.

    The identifier LLM under heavy output load sometimes emits valid-shape
    candidates with `source_page_id` populated but `content_block_ids: []`.
    `_validate_candidate` only checks list shape, not non-emptiness, so
    these pass through and then fail Stage 2-draft with
    `no_cited_blocks_resolvable`.

    We rebuild content_block_ids from the chunk's actual block IDs on the
    cited page(s). The LLM's intent was clearly page-level provenance.
    """
    page_to_blocks: dict[str, list[str]] = {}
    for doc in chunk_page_corpus:
        for page in doc.get("pages", []) or []:
            page_id = str(page.get("source_page_id", ""))
            if not page_id:
                continue
            block_ids = [
                str(b.get("content_block_id"))
                for b in page.get("blocks", []) or []
                if b.get("content_block_id")
            ]
            if block_ids:
                page_to_blocks.setdefault(page_id, []).extend(block_ids)

    out: list[dict[str, Any]] = []
    for cand in candidates:
        prov = cand.get("source_provenance", []) or []
        if not isinstance(prov, list):
            out.append(cand)
            continue
        modified = False
        new_prov: list[dict[str, Any]] = []
        for entry in prov:
            if not isinstance(entry, dict):
                new_prov.append(entry)
                continue
            block_ids = entry.get("content_block_ids", []) or []
            if block_ids:
                new_prov.append(entry)
                continue
            page_id = str(entry.get("source_page_id", ""))
            chunk_blocks = page_to_blocks.get(page_id, [])
            if not chunk_blocks:
                new_prov.append(entry)
                continue
            new_entry = dict(entry)
            new_entry["content_block_ids"] = chunk_blocks
            new_prov.append(new_entry)
            modified = True
        if modified:
            cand_copy = dict(cand)
            cand_copy["source_provenance"] = new_prov
            out.append(cand_copy)
            logger.info(
                "Stage 2 backfill: candidate %r had empty content_block_ids; "
                "filled from chunk's page-level blocks",
                cand.get("proposed_title", ""),
            )
        else:
            out.append(cand)
    return out


@dataclass(frozen=True)
class StageCResult:
    """Summary of one Stage C run for one ingestion run."""

    ingestion_run_id: UUID
    candidates_emitted: int
    # Count of candidates that have at least one quality flag set. Per the
    # architecture reset, flagged candidates are still emitted (the
    # dashboard surfaces them as "needs attention" rather than the pipeline
    # silently dropping them).
    candidates_flagged: int
    flag_counts: dict[str, int]
    estimated_corpus_tokens: int
    # Chunking telemetry. Always populated (chunker always runs).
    chunks_attempted: int = 0
    chunks_succeeded: int = 0
    chunks_failed: int = 0
    # Count of candidates flagged for cross-chunk reviewer attention
    # (trigram-similar titles from different chunks).
    cross_chunk_review_count: int = 0
    ingestion_instructions_present: bool = False


def _ingestion_instructions_from_documents(documents: list[Any]) -> str | None:
    """Return the first non-null ingestion_instructions across loaded documents."""
    for doc in documents:
        instructions = getattr(doc, "ingestion_instructions", None)
        if instructions:
            return str(instructions)
    return None


class StageCOrchestrator:
    """Stage 2 orchestrator — chunked corpus identification."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        identifier: ModuleIdentifier | None = None,
    ) -> None:
        self._session = session
        self._source_repo = SourceRepository(session)
        self._candidate_repo = ModuleCandidateRepository(session)
        self._identifier = identifier or ModuleIdentifier()

    async def run(
        self,
        *,
        ingestion_run_id: UUID,
        source_document_ids: list[UUID],
    ) -> StageCResult:
        """Execute Stage 2 for one workspace (ingestion run)."""
        documents = await self._load_documents(source_document_ids)
        if not documents:
            logger.info("Stage 2: no documents in workspace, no candidates emitted")
            return StageCResult(
                ingestion_run_id=ingestion_run_id,
                candidates_emitted=0,
                candidates_flagged=0,
                flag_counts={},
                estimated_corpus_tokens=0,
            )

        await self._ensure_content_blocks(documents)

        published, _ = await self._load_published_modules()
        (
            document_outlines,
            page_corpus,
            valid_block_ids,
            valid_page_ids,
            block_token_counts,
            block_headings,
        ) = await self._build_corpus_payload(documents)
        content_domains = {d["content_domain"] for d in page_corpus}
        estimated_tokens = estimate_corpus_tokens(page_corpus)
        ingestion_instructions = _ingestion_instructions_from_documents(documents)

        # Chunk into token-budgeted, content-disjoint slices.
        chunks = chunk_by_token_budget(page_corpus, document_outlines)
        chunks_attempted = len(chunks)
        chunks_succeeded = 0
        chunks_failed = 0

        settings = get_settings()
        sem = asyncio.Semaphore(settings.stage_c_section_concurrency)

        async def _identify_one(
            chunk: Any,
        ) -> tuple[Any, list[dict[str, Any]] | None]:
            async with sem:
                try:
                    result = await self._identifier.identify(
                        content_domains=content_domains,
                        already_published_modules=published,
                        document_outlines=document_outlines,
                        page_corpus=chunk.page_corpus,
                        valid_block_ids=valid_block_ids,
                        valid_page_ids=valid_page_ids,
                        ingestion_instructions=ingestion_instructions,
                    )
                    backfilled = _backfill_empty_block_ids(result.candidates, chunk.page_corpus)
                    return chunk, backfilled
                except Exception as exc:
                    # Catch broadly — one chunk's failure (transport
                    # timeout, JSON parse, validator rejection, anything)
                    # must not abort the other parallel chunks via gather
                    # cancellation. Cancelled in-flight DB queries leave
                    # asyncpg in "another operation in progress" state and
                    # subsequent rollback fails. Treat any chunk error as
                    # a soft chunk failure.
                    logger.warning(
                        "Stage 2 chunk %s failed (%s): %s — skipping",
                        chunk.chunk_id,
                        type(exc).__name__,
                        exc,
                    )
                    return chunk, None

        # `return_exceptions=True` is belt-and-braces: even if a future
        # change to `_identify_one` lets an exception escape, we still
        # collect all results rather than cancelling siblings.
        per_chunk = await asyncio.gather(*(_identify_one(c) for c in chunks), return_exceptions=True)
        # Normalise any exception that did escape into the same
        # (chunk, None) shape the rest of this method expects.
        normalised: list[tuple[Any, list[dict[str, Any]] | None]] = []
        for idx, item in enumerate(per_chunk):
            if isinstance(item, BaseException):
                logger.warning(
                    "Stage 2 chunk %s escaped into gather: %s — treating as failed",
                    chunks[idx].chunk_id,
                    item,
                )
                normalised.append((chunks[idx], None))
            else:
                normalised.append(item)
        per_chunk = normalised

        # Build (chunk_id, candidates) pairs for cross-chunk dedup.
        chunked_candidates: list[tuple[str, list[dict[str, Any]]]] = []
        for chunk, candidates in per_chunk:
            if candidates is None:
                chunks_failed += 1
                continue
            chunks_succeeded += 1
            logger.info(
                "Stage 2 chunk %s produced %d candidates",
                chunk.chunk_id,
                len(candidates),
            )
            chunked_candidates.append((chunk.chunk_id, candidates))

        # Cross-chunk title dedup + near-duplicate flagging.
        raw_candidates = dedup_and_flag_cross_chunk(chunked_candidates)
        cross_chunk_review_count = sum(1 for c in raw_candidates if c.get("_cross_chunk_review"))

        # Quality-flag pass (advisory; never gates publish).
        outline_section_count = sum(len(o.get("sections", [])) for o in document_outlines)
        flag_counts: dict[str, int] = {}
        candidates_flagged = 0
        for cand in raw_candidates:
            decision: FilterDecision = evaluate_candidate(
                source_provenance=cand.get("source_provenance", []),
                cited_block_token_counts=block_token_counts,
                cited_block_headings=block_headings,
                outline_section_count=outline_section_count,
            )
            existing_flags = list(cand.get("_quality_flags") or [])
            existing_flags.extend(decision.fail_reasons)
            if cand.get("_cross_chunk_review"):
                existing_flags.append("cross_chunk_near_duplicate")
            cand["_quality_flags"] = existing_flags
            if existing_flags:
                candidates_flagged += 1
            for reason in existing_flags:
                flag_counts[reason] = flag_counts.get(reason, 0) + 1

        # Persist each candidate as a module_candidate_draft row.
        for cand in raw_candidates:
            flags = cand.get("_quality_flags") or []
            quality_flags = {"flags": flags} if flags else None
            await self._candidate_repo.create_candidate(
                ingestion_run_id=ingestion_run_id,
                proposed_title=cand["proposed_title"],
                scope_summary=cand["scope_summary"],
                description_localized=candidate_description_localized(cand),
                source_provenance=cand["source_provenance"],
                estimated_card_count=int(cand["estimated_card_count"]),
                estimated_quiz_count=int(cand["estimated_quiz_count"]),
                proposed_module_type=cand["proposed_module_type"],
                quality_flags=quality_flags,
                clinical_review_notes=cand.get("clinical_review_notes"),
                previous_practice_summary=cand.get("previous_practice_summary"),
                current_practice_summary=cand.get("current_practice_summary"),
                rationale_summary=cand.get("rationale_summary"),
                ingestion_instruction_rationale=cand.get("ingestion_instruction_rationale"),
            )

        logger.info(
            "Stage 2 ingestion_run=%s: emitted=%d flagged=%d cross_chunk_review=%d "
            "(chunks: %d/%d succeeded; flag_counts=%s)",
            ingestion_run_id,
            len(raw_candidates),
            candidates_flagged,
            cross_chunk_review_count,
            chunks_succeeded,
            chunks_attempted,
            flag_counts,
        )
        if ingestion_instructions and len(raw_candidates) == 0:
            logger.info(
                "Stage 2 ingestion_run=%s: ingestion instructions present but "
                "zero instruction-aligned candidates were produced",
                ingestion_run_id,
            )
        return StageCResult(
            ingestion_run_id=ingestion_run_id,
            candidates_emitted=len(raw_candidates),
            candidates_flagged=candidates_flagged,
            flag_counts=flag_counts,
            estimated_corpus_tokens=estimated_tokens,
            chunks_attempted=chunks_attempted,
            chunks_succeeded=chunks_succeeded,
            chunks_failed=chunks_failed,
            cross_chunk_review_count=cross_chunk_review_count,
            ingestion_instructions_present=ingestion_instructions is not None,
        )

    # ── Internal helpers ────────────────────────────────────────────────

    async def _load_documents(self, source_document_ids: list[UUID]) -> list[Any]:
        docs = []
        for doc_id in source_document_ids:
            doc = await self._source_repo.get_source_document(doc_id)
            if doc is not None:
                docs.append(doc)
        return docs

    async def _ensure_content_blocks(self, documents: list[Any]) -> None:
        """Parse markdown_content into ContentBlock rows for any page that
        doesn't already have them. Idempotent — skip pages that already do."""
        for doc in documents:
            pages = await self._source_repo.list_pages_for_document(doc.id)
            for page in pages:
                existing = await self._source_repo.list_blocks_for_page(page.id)
                if existing:
                    continue
                parsed_blocks = parse_page_blocks(page.markdown_content or "")
                if not parsed_blocks:
                    continue
                rows = [
                    pb.to_repo_dict(
                        source_page_id=page.id,
                        content_language=page.language_detected,
                    )
                    for pb in parsed_blocks
                ]
                await self._source_repo.bulk_create_content_blocks(rows)

    async def _load_published_modules(self) -> tuple[list[dict[str, Any]], set[UUID]]:
        result = await self._session.execute(
            select(Module, ModuleFamily.module_code)
            .join(ModuleFamily, Module.module_family_id == ModuleFamily.id)
            .where(Module.lifecycle_status == "published")
        )
        rows = result.all()
        published = [
            {
                "module_code": code,
                "module_family_id": str(mod.module_family_id),
                "title_localized": mod.title_localized,
                "domain": mod.domain,
                "module_type": mod.module_type,
            }
            for mod, code in rows
        ]
        family_ids = {mod.module_family_id for mod, _ in rows}
        return published, family_ids

    async def _build_corpus_payload(
        self,
        documents: list[Any],
    ) -> tuple[
        list[dict[str, Any]],  # document_outlines
        list[dict[str, Any]],  # page_corpus
        set[UUID],  # valid_block_ids
        set[UUID],  # valid_page_ids
        dict[UUID, int],  # block_token_counts
        dict[UUID, list[str]],  # block_headings
    ]:
        document_outlines: list[dict[str, Any]] = []
        page_corpus: list[dict[str, Any]] = []
        valid_block_ids: set[UUID] = set()
        valid_page_ids: set[UUID] = set()
        block_token_counts: dict[UUID, int] = {}
        block_headings: dict[UUID, list[str]] = {}

        for doc in documents:
            pages = await self._source_repo.list_pages_for_document(doc.id)
            doc_pages_payload: list[dict[str, Any]] = []
            for page in pages:
                valid_page_ids.add(page.id)
                blocks = await self._source_repo.list_blocks_for_page(page.id)
                block_payload: list[dict[str, Any]] = []
                for b in blocks:
                    valid_block_ids.add(b.id)
                    tokens = estimate_token_count(b.content_text)
                    block_token_counts[b.id] = tokens
                    if b.heading_path_jsonb is not None:
                        block_headings[b.id] = list(b.heading_path_jsonb)
                    block_payload.append(
                        {
                            "content_block_id": str(b.id),
                            "block_type": b.block_type,
                            "content_text": b.content_text,
                        }
                    )
                doc_pages_payload.append(
                    {
                        "source_page_id": str(page.id),
                        "page_number": page.page_number,
                        "blocks": block_payload,
                    }
                )
            page_corpus.append(
                {
                    "source_document_id": str(doc.id),
                    "content_domain": doc.content_domain,
                    "primary_language": doc.primary_language,
                    "pages": doc_pages_payload,
                }
            )
            outline_jsonb = doc.outline_jsonb or {"sections": []}
            document_outlines.append(
                {
                    "source_document_id": str(doc.id),
                    "outline_method": doc.outline_method,
                    "sections": outline_jsonb.get("sections", []),
                }
            )
        return (
            document_outlines,
            page_corpus,
            valid_block_ids,
            valid_page_ids,
            block_token_counts,
            block_headings,
        )


__all__ = ["StageCOrchestrator", "StageCResult"]
