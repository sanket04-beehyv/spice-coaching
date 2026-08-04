"""Golden dataset loading for RAG chatbot end-to-end evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from eval.rag.corpus import CardCorpusDoc, lookup_card_by_id
from eval.rag.dataset import (
    _is_canonical_golden_item,
    _parse_golden_answerable,
    _parse_golden_module_ids,
    _parse_golden_source_card_ids,
    load_golden_json_array,
)

QuestionLang = Literal["en", "bn"]
Answerable = Literal["yes", "no", "partial"]

_OUT_OF_SCOPE_CATEGORY = "out-of-scope"


@dataclass(frozen=True)
class RagGoldenRecord:
    id: str
    category: str
    language: QuestionLang
    query: str
    expected_answer: str
    expected_module_ids: tuple[UUID, ...]
    is_out_of_scope: bool
    answerable: Answerable
    expected_card_ids: tuple[UUID, ...]


def _is_out_of_scope_category(category: str) -> bool:
    return category.strip().casefold() == _OUT_OF_SCOPE_CATEGORY


def _parse_expected_module_ids(raw: object, *, record_id: str) -> tuple[UUID, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"Record {record_id}: expected_module_id must be a list or null")
    return tuple(UUID(str(module_id)) for module_id in raw)


def _derive_is_out_of_scope(*, category: str, expected_module_ids: tuple[UUID, ...]) -> bool:
    if not expected_module_ids:
        return True
    return _is_out_of_scope_category(category)


def _validate_expected_module_id_scope(
    *,
    category: str,
    expected_module_ids: tuple[UUID, ...],
    record_id: str,
) -> None:
    if _is_out_of_scope_category(category) and expected_module_ids:
        raise ValueError(
            f"Record {record_id}: out-of-scope records must have expected_module_id null or empty"
        )


def _parse_answerable_literal(raw: object, *, module_ids: list[UUID]) -> Answerable:
    answerable = str(raw).strip().casefold() if raw is not None else "yes"
    if answerable == "no":
        return "no"
    if answerable == "partial":
        return "partial"
    if not module_ids:
        return "no"
    return "yes"


def category_slug(category: str) -> str:
    slug = category.strip().casefold()
    slug = slug.replace("—", "-").replace("–", "-")
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


def _load_canonical_rag_record(idx: int, item: dict[str, object]) -> RagGoldenRecord:
    question_bn = item.get("question_bn")
    expected_answer_bn = item.get("expected_answer_bn")
    if not question_bn:
        raise ValueError(f"Record {idx} must include question_bn")
    if not expected_answer_bn:
        raise ValueError(f"Record {idx} must include expected_answer_bn")

    record_id = str(item.get("id") or f"q_{idx + 1:03d}")
    module_ids = _parse_golden_module_ids(item.get("module_id"), record_id=record_id)
    _is_answerable, is_out_of_scope = _parse_golden_answerable(
        item.get("answerable"),
        module_ids=module_ids,
    )
    source_card_ids = _parse_golden_source_card_ids(item.get("source_card_id"), record_id=record_id)

    return RagGoldenRecord(
        id=record_id,
        category=str(item.get("query_type", "")),
        language="bn",
        query=str(question_bn),
        expected_answer=str(expected_answer_bn),
        expected_module_ids=tuple(module_ids),
        is_out_of_scope=is_out_of_scope,
        answerable=_parse_answerable_literal(item.get("answerable"), module_ids=module_ids),
        expected_card_ids=source_card_ids,
    )


def load_rag_golden_dataset(path: Path) -> list[RagGoldenRecord]:
    if not path.is_file():
        raise FileNotFoundError(f"RAG golden dataset not found: {path}")

    raw = load_golden_json_array(path)

    records: list[RagGoldenRecord] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Record {idx} must be an object")

        if _is_canonical_golden_item(item):
            records.append(_load_canonical_rag_record(idx, item))
            continue

        query = item.get("query")
        expected_answer = item.get("expected_answer")
        language = item.get("language")
        if not query:
            raise ValueError(f"Record {idx} must include query")
        if not expected_answer:
            raise ValueError(f"Record {idx} must include expected_answer")
        if language not in ("en", "bn"):
            raise ValueError(f"Record {idx} must include language 'en' or 'bn'")

        record_id = str(item.get("id", idx + 1))
        category = str(item.get("category", ""))
        expected_module_ids = _parse_expected_module_ids(
            item.get("expected_module_id"),
            record_id=record_id,
        )
        _validate_expected_module_id_scope(
            category=category,
            expected_module_ids=expected_module_ids,
            record_id=record_id,
        )
        out_of_scope = _derive_is_out_of_scope(
            category=category,
            expected_module_ids=expected_module_ids,
        )
        answerable: Answerable = "no" if out_of_scope else "yes"

        records.append(
            RagGoldenRecord(
                id=record_id,
                category=category,
                language=cast(QuestionLang, language),
                query=str(query),
                expected_answer=str(expected_answer),
                expected_module_ids=expected_module_ids,
                is_out_of_scope=out_of_scope,
                answerable=answerable,
                expected_card_ids=(),
            )
        )
    return records


def validate_expected_module_ids(
    records: list[RagGoldenRecord],
    published_module_ids: set[UUID],
) -> list[str]:
    """Warn when a golden UUID is not in the live published corpus."""
    warnings: list[str] = []
    for record in records:
        missing = [
            module_id for module_id in record.expected_module_ids if module_id not in published_module_ids
        ]
        if missing:
            warnings.append(f"{record.id}: expected_module_id not in published corpus: {missing!r}")
    return warnings


def validate_expected_card_ids(
    records: list[RagGoldenRecord],
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
) -> list[str]:
    """Warn when a golden card UUID is not in the live published corpus."""
    warnings: list[str] = []
    for record in records:
        if record.is_out_of_scope or not record.expected_card_ids:
            continue
        missing = [
            card_id
            for card_id in record.expected_card_ids
            if lookup_card_by_id(card_id, list(record.expected_module_ids), cards_by_module) is None
        ]
        if missing:
            warnings.append(f"{record.id}: expected_card_id not in published corpus: {missing!r}")
    return warnings
