"""Stage 2 token-budget chunker.

Replaces the prior outline-partitioner (which sent overlapping pages to the
identifier when the outline had thin or duplicate sections — see SK manual
page 90 with 9 numbered checklist items all marked `#` headings).

The new strategy: walk pages in order, accumulate into the current chunk
until ~`stage_c_chunk_target_tokens` tokens; when over the target, look for
the nearest outline section boundary within `stage_c_chunk_window_pct` of
the cap and break there. Chunks are content-disjoint — every page lands in
exactly one chunk.

After all chunks are processed, candidates are deduplicated by normalised
title (deterministic) and flagged for cross-chunk reviewer attention when
near-duplicate titles appear across chunks (trigram similarity).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from platform_service.config import get_settings
from platform_service.services.text_similarity import normalise_title, trigram_similarity
from platform_service.services.token_estimation import estimate_token_count

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CorpusChunk:
    """A token-budgeted, content-disjoint slice of the corpus."""

    chunk_id: str
    page_range: tuple[int, int]
    page_corpus: list[dict[str, Any]]
    estimated_tokens: int


def estimate_corpus_tokens(page_corpus: list[dict[str, Any]]) -> int:
    """Sum estimated token counts across all blocks in the page_corpus."""
    total = 0
    for doc in page_corpus:
        for page in doc.get("pages", []):
            for block in page.get("blocks", []):
                total += estimate_token_count(block.get("content_text", ""))
    return total


def _page_token_count(page: dict[str, Any]) -> int:
    """Sum estimated token counts for one page's blocks."""
    return sum(estimate_token_count(b.get("content_text", "")) for b in page.get("blocks", []) or [])


def chunk_by_token_budget(
    page_corpus: list[dict[str, Any]],
    document_outlines: list[dict[str, Any]],
) -> list[CorpusChunk]:
    """Walk pages in document order, accumulate up to the token target;
    break when at/over target, or when adding the next page would overshoot
    by more than `stage_c_chunk_window_pct`.

    For SK manual (~250K tokens, target 60K, window 10%): produces ~4 chunks.
    For a small SOP under the target: produces 1 chunk.

    `document_outlines` is accepted for API symmetry with the prior
    partitioner; the current chunker breaks purely on token thresholds.
    Outline-boundary-preferred breaking was tried earlier but a single
    dense vision-extracted page can be 30K+ tokens, so waiting for an
    outline boundary that may never come within window left chunks at
    ~70K — well past target. Always break on threshold.
    """
    del document_outlines  # reserved for future boundary-aware breaking
    settings = get_settings()
    target = settings.stage_c_chunk_target_tokens
    window_frac = settings.stage_c_chunk_window_pct
    window = int(target * window_frac)

    chunks: list[CorpusChunk] = []
    chunk_idx = 0
    for doc in page_corpus:
        doc_id = doc.get("source_document_id")
        authority = doc.get("content_domain")
        primary_language = doc.get("primary_language")
        pages = sorted(
            (p for p in doc.get("pages", []) or []),
            key=lambda p: int(p.get("page_number", 0)),
        )
        if not pages:
            continue

        current_pages: list[dict[str, Any]] = []
        current_tokens = 0
        for page in pages:
            page_tokens = _page_token_count(page)

            # Decide whether to break BEFORE adding this page. Break when:
            # - current chunk is already at/over target, OR
            # - adding this page would push past (target + window).
            would_overshoot = (current_tokens + page_tokens) > (target + window)
            past_target = current_tokens >= target

            if current_pages and (past_target or would_overshoot):
                # Close out the current chunk.
                chunk_idx += 1
                start = int(current_pages[0]["page_number"])
                end = int(current_pages[-1]["page_number"])
                chunks.append(
                    CorpusChunk(
                        chunk_id=f"chunk-{chunk_idx}",
                        page_range=(start, end),
                        page_corpus=[
                            {
                                "source_document_id": doc_id,
                                "content_domain": authority,
                                "primary_language": primary_language,
                                "pages": current_pages,
                            }
                        ],
                        estimated_tokens=current_tokens,
                    )
                )
                current_pages = []
                current_tokens = 0

            current_pages.append(page)
            current_tokens += page_tokens

        # Close out the trailing chunk.
        if current_pages:
            chunk_idx += 1
            start = int(current_pages[0]["page_number"])
            end = int(current_pages[-1]["page_number"])
            chunks.append(
                CorpusChunk(
                    chunk_id=f"chunk-{chunk_idx}",
                    page_range=(start, end),
                    page_corpus=[
                        {
                            "source_document_id": doc_id,
                            "content_domain": authority,
                            "primary_language": primary_language,
                            "pages": current_pages,
                        }
                    ],
                    estimated_tokens=current_tokens,
                )
            )

    logger.info(
        "Stage 2 chunker: %d chunks from %d-doc corpus (target=%d tokens, window=%d%%)",
        len(chunks),
        len(page_corpus),
        target,
        int(window_frac * 100),
    )
    for c in chunks:
        logger.info(
            "Stage 2 chunk %s: pages=%s tokens=%d",
            c.chunk_id,
            c.page_range,
            c.estimated_tokens,
        )
    return chunks


# ── Title dedup + cross-chunk near-dup flagging ─────────────────────────


def dedup_and_flag_cross_chunk(
    chunked_candidates: list[tuple[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Collapse same-titled candidates across chunks, flag near-dups for review.

    Input: list of (chunk_id, candidates_for_that_chunk) pairs.
    Output: deduplicated candidate list. Each candidate gains:
        - `_chunk_lineage`: list of chunk_ids it appeared in
        - `_cross_chunk_review`: True iff a different-titled candidate from
          another chunk shares trigram similarity ≥ threshold

    Same-title (post-normalisation) candidates from different chunks are
    merged: provenance is unioned, lineage is unioned, the first occurrence's
    other fields win.
    """
    settings = get_settings()
    threshold = settings.stage_c_cross_chunk_similarity_threshold

    # Step 1: dedup by normalised title across chunks. Preserves chunk lineage.
    by_title: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for chunk_id, candidates in chunked_candidates:
        for cand in candidates:
            key = normalise_title(cand.get("proposed_title", ""))
            if not key:
                # Empty title — keep with synthetic key.
                key = f"__empty_{id(cand)}"
            if key in by_title:
                rep = by_title[key]
                # Union provenance.
                seen_keys: set[tuple[str, str, str]] = set()
                for entry in rep.get("source_provenance", []) or []:
                    if not isinstance(entry, dict):
                        continue
                    doc = str(entry.get("source_document_id"))
                    page = str(entry.get("source_page_id"))
                    for block in entry.get("content_block_ids", []) or []:
                        seen_keys.add((doc, page, str(block)))
                merged_prov = list(rep.get("source_provenance", []) or [])
                for entry in cand.get("source_provenance", []) or []:
                    if not isinstance(entry, dict):
                        continue
                    doc = entry.get("source_document_id")
                    page = entry.get("source_page_id")
                    for block in entry.get("content_block_ids", []) or []:
                        tup = (str(doc), str(page), str(block))
                        if tup in seen_keys:
                            continue
                        seen_keys.add(tup)
                        merged_prov.append(
                            {
                                "source_document_id": doc,
                                "source_page_id": page,
                                "content_block_ids": [block],
                            }
                        )
                rep["source_provenance"] = merged_prov
                # Track lineage.
                rep.setdefault("_chunk_lineage", []).append(chunk_id)
                logger.info(
                    "Stage 2 cross-chunk dedup: collapsed candidate %r (also in %s)",
                    cand.get("proposed_title", ""),
                    chunk_id,
                )
            else:
                cand_copy = dict(cand)
                cand_copy["_chunk_lineage"] = [chunk_id]
                by_title[key] = cand_copy
                order.append(key)

    deduped = [by_title[k] for k in order]

    # Step 2: flag near-duplicates across chunks (different titles, similar
    # content). Pairs with similarity >= threshold get _cross_chunk_review=True.
    # We compare every pair once and mark both sides.
    for i, a in enumerate(deduped):
        a_title = a.get("proposed_title", "")
        a_chunks = set(a.get("_chunk_lineage", []) or [])
        for b in deduped[i + 1 :]:
            b_chunks = set(b.get("_chunk_lineage", []) or [])
            # Only flag when the candidates came from DIFFERENT chunks
            # (within-chunk near-dups are the LLM's call to make).
            if a_chunks & b_chunks:
                continue
            sim = trigram_similarity(a_title, b.get("proposed_title", ""))
            if sim >= threshold:
                a["_cross_chunk_review"] = True
                b["_cross_chunk_review"] = True
                logger.info(
                    "Stage 2 cross-chunk near-dup flagged: %r vs %r similarity=%.2f",
                    a_title,
                    b.get("proposed_title", ""),
                    sim,
                )

    return deduped
