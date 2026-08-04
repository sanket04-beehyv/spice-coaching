"""In-memory BM25 index over module and card corpus documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from rank_bm25 import BM25Okapi

from eval.rag.corpus import CardCorpusDoc, CorpusDoc


class _SearchableDoc(Protocol):
    text: str


@dataclass(frozen=True)
class Hit:
    rank: int
    module_id: UUID
    primary_title: str | None
    title_en: str | None
    title_bn: str | None
    bm25_score: float
    text_preview: str


@dataclass(frozen=True)
class CardHit:
    rank: int
    module_id: UUID
    card_id: UUID
    card_index: int
    primary_title: str | None
    title_en: str | None
    title_bn: str | None
    bm25_score: float
    text_preview: str


def tokenize(text: str) -> list[str]:
    return text.lower().split()


class Bm25Index:
    def __init__(self, docs: list[_SearchableDoc]) -> None:
        self._docs = docs
        self._corpus_tokens = [tokenize(doc.text) for doc in docs]
        self._bm25 = BM25Okapi(self._corpus_tokens) if docs else None

    @property
    def doc_count(self) -> int:
        return len(self._docs)

    def search(self, query: str, *, k: int) -> list[Hit]:
        if not self._docs or self._bm25 is None:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        ranked_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        hits: list[Hit] = []
        for rank, idx in enumerate(ranked_indices[:k], start=1):
            doc = self._docs[idx]
            if not isinstance(doc, CorpusDoc):
                raise TypeError("Bm25Index.search expects CorpusDoc documents")
            preview = doc.text[:120].replace("\n", " ")
            hits.append(
                Hit(
                    rank=rank,
                    module_id=doc.module_id,
                    primary_title=doc.primary_title,
                    title_en=doc.title_en,
                    title_bn=doc.title_bn,
                    bm25_score=float(scores[idx]),
                    text_preview=preview,
                )
            )
        return hits


class CardBm25Index:
    def __init__(self, docs: list[CardCorpusDoc]) -> None:
        self._docs = docs
        self._corpus_tokens = [tokenize(doc.text) for doc in docs]
        self._bm25 = BM25Okapi(self._corpus_tokens) if docs else None

    @property
    def doc_count(self) -> int:
        return len(self._docs)

    def search(self, query: str, *, k: int) -> list[CardHit]:
        if not self._docs or self._bm25 is None:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        ranked_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        hits: list[CardHit] = []
        for rank, idx in enumerate(ranked_indices[:k], start=1):
            doc = self._docs[idx]
            preview = doc.text[:120].replace("\n", " ")
            hits.append(
                CardHit(
                    rank=rank,
                    module_id=doc.module_id,
                    card_id=doc.card_id,
                    card_index=doc.card_index,
                    primary_title=doc.primary_title,
                    title_en=doc.title_en,
                    title_bn=doc.title_bn,
                    bm25_score=float(scores[idx]),
                    text_preview=preview,
                )
            )
        return hits


def build_card_indexes(
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
) -> dict[UUID, CardBm25Index]:
    return {module_id: CardBm25Index(cards) for module_id, cards in cards_by_module.items() if cards}
