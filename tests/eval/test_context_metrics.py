"""Unit tests for context-proxy metrics."""

from __future__ import annotations

from uuid import UUID

from eval.rag.context_metrics import (
    compute_context_metrics,
    gold_card_hit,
)
from eval.rag.corpus import CardCorpusDoc
from eval.rag.rag_dataset import RagGoldenRecord

_GOLD_CARD_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_OTHER_CARD_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_MODULE_ID = UUID("11111111-1111-1111-1111-111111111111")


def _record(**kwargs: object) -> RagGoldenRecord:
    defaults = {
        "id": "Q001",
        "category": "Factual",
        "language": "bn",
        "query": "test",
        "expected_answer": "answer",
        "expected_module_ids": (_MODULE_ID,),
        "is_out_of_scope": False,
        "answerable": "yes",
        "expected_card_ids": (_GOLD_CARD_ID,),
    }
    defaults.update(kwargs)
    return RagGoldenRecord(**defaults)  # type: ignore[arg-type]


def test_gold_card_hit_matches_card_id() -> None:
    assert gold_card_hit((_GOLD_CARD_ID,), [_GOLD_CARD_ID, _OTHER_CARD_ID]) == 1.0
    assert gold_card_hit((_GOLD_CARD_ID,), [_OTHER_CARD_ID]) == 0.0


def test_compute_context_metrics_skips_out_of_scope() -> None:
    record = _record(is_out_of_scope=True, answerable="no", expected_card_ids=())
    result = compute_context_metrics(
        record=record,
        retrieved_module_ids=[_MODULE_ID],
        cards_by_module={},
        k=5,
    )
    assert result is None


def test_compute_context_metrics_with_corpus_cards() -> None:
    record = _record(expected_card_ids=(_GOLD_CARD_ID,))
    cards_by_module = {
        _MODULE_ID: [
            CardCorpusDoc(
                module_id=_MODULE_ID,
                card_id=_GOLD_CARD_ID,
                card_index=0,
                card_family_id=None,
                primary_title="Target Card",
                title_en=None,
                title_bn=None,
                text="body",
            ),
            CardCorpusDoc(
                module_id=_MODULE_ID,
                card_id=_OTHER_CARD_ID,
                card_index=1,
                card_family_id=None,
                primary_title="Other Card",
                title_en=None,
                title_bn=None,
                text="other",
            ),
        ]
    }
    metrics = compute_context_metrics(
        record=record,
        retrieved_module_ids=[_MODULE_ID],
        cards_by_module=cards_by_module,
        k=5,
    )
    assert metrics is not None
    assert metrics["gold_card_hit"] == 1.0
    assert metrics["card_recall_at_k"] == 1.0
    assert metrics["card_mrr"] == 1.0
