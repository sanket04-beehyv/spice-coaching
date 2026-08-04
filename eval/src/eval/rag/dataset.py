"""Golden dataset loading for batch retrieval evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from eval.rag.corpus import CardCorpusDoc, CorpusDoc, lookup_card_by_id

QuestionLang = Literal["en", "bn"]

_OUT_OF_SCOPE_CATEGORY = "out-of-scope"


@dataclass(frozen=True)
class GoldenRecord:
    id: str
    category: str
    question: str
    expected_module: str | None
    relevant_module_ids: list[UUID]
    is_answerable: bool
    expected_module_id: UUID | None = None
    expected_card_ids: tuple[UUID, ...] = ()
    question_lang: QuestionLang | None = None
    is_out_of_scope: bool = False


@dataclass(frozen=True)
class GoldenResolutionIssue:
    record_id: str
    message: str


def _is_out_of_scope_category(category: str) -> bool:
    return category.strip().casefold() == _OUT_OF_SCOPE_CATEGORY


def _has_expected_module_label(
    *,
    expected_module: str | None,
    relevant_module_ids: list[UUID],
    expected_module_id: UUID | None,
) -> bool:
    if expected_module_id is not None or relevant_module_ids:
        return True
    return bool(expected_module and expected_module.strip())


def _derive_is_out_of_scope(
    *,
    category: str,
    expected_module: str | None,
    relevant_module_ids: list[UUID],
    expected_module_id: UUID | None,
) -> bool:
    if not _has_expected_module_label(
        expected_module=expected_module,
        relevant_module_ids=relevant_module_ids,
        expected_module_id=expected_module_id,
    ):
        return True
    return _is_out_of_scope_category(category)


def build_module_title_index(docs: list[CorpusDoc]) -> dict[str, UUID]:
    """Map module titles (primary, English, and Bangla) to module IDs."""
    index: dict[str, UUID] = {}
    for doc in docs:
        for title in (doc.primary_title, doc.title_en, doc.title_bn):
            if title:
                index[title.strip()] = doc.module_id
    return index


def _unresolved_module_title_issue(
    record: GoldenRecord,
    title_index: dict[str, UUID],
) -> GoldenResolutionIssue | None:
    if record.is_out_of_scope:
        return None
    if record.expected_module_id is not None:
        return None
    if record.relevant_module_ids or not record.expected_module:
        return None

    title = record.expected_module.strip()
    if title_index.get(title) is not None:
        return None

    return GoldenResolutionIssue(
        record_id=record.id,
        message=f"{record.id}: no published module titled {title!r}",
    )


def _unresolved_expected_card_issue(
    record: GoldenRecord,
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
) -> GoldenResolutionIssue | None:
    if record.is_out_of_scope or not record.expected_card_ids:
        return None
    if not record.relevant_module_ids:
        return None

    missing = [
        card_id
        for card_id in record.expected_card_ids
        if lookup_card_by_id(card_id, record.relevant_module_ids, cards_by_module) is None
    ]
    if not missing:
        return None

    label = str(missing[0]) if len(missing) == 1 else [str(card_id) for card_id in missing]
    return GoldenResolutionIssue(
        record_id=record.id,
        message=(
            f"{record.id}: expected_card_id {label!r} not found in modules {record.relevant_module_ids}"
        ),
    )


def collect_golden_resolution_issues(
    records: list[GoldenRecord],
    docs: list[CorpusDoc],
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
) -> list[GoldenResolutionIssue]:
    """Return label and card resolution failures for golden records."""
    title_index = build_module_title_index(docs)
    issues: list[GoldenResolutionIssue] = []
    for record in records:
        label_issue = _unresolved_module_title_issue(record, title_index)
        if label_issue is not None:
            issues.append(label_issue)
        card_issue = _unresolved_expected_card_issue(record, cards_by_module)
        if card_issue is not None:
            issues.append(card_issue)
    return issues


def unresolvable_golden_record_ids(issues: list[GoldenResolutionIssue]) -> set[str]:
    return {issue.record_id for issue in issues}


def resolve_golden_labels(
    records: list[GoldenRecord],
    docs: list[CorpusDoc],
) -> tuple[list[GoldenRecord], list[str]]:
    """Fill ``relevant_module_ids`` from ``expected_module`` using the live corpus."""
    title_index = build_module_title_index(docs)
    resolved: list[GoldenRecord] = []
    warnings: list[str] = []

    for record in records:
        if record.is_out_of_scope:
            resolved.append(record)
            continue
        if record.expected_module_id is not None:
            resolved.append(record)
            continue
        if record.relevant_module_ids or not record.expected_module:
            resolved.append(record)
            continue

        label_issue = _unresolved_module_title_issue(record, title_index)
        if label_issue is not None:
            warnings.append(label_issue.message)
            resolved.append(record)
            continue

        title = record.expected_module.strip()
        module_id = title_index[title]
        resolved.append(replace(record, relevant_module_ids=[module_id]))

    return resolved, warnings


def lookup_expected_card_index(
    *,
    card_id: UUID,
    module_ids: list[UUID],
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
) -> int | None:
    card = lookup_card_by_id(card_id, module_ids, cards_by_module)
    return card.card_index if card is not None else None


def collect_card_resolution_warnings(
    records: list[GoldenRecord],
    cards_by_module: dict[UUID, list[CardCorpusDoc]],
) -> list[str]:
    warnings: list[str] = []
    for record in records:
        card_issue = _unresolved_expected_card_issue(record, cards_by_module)
        if card_issue is not None:
            warnings.append(card_issue.message)
    return warnings


def _is_canonical_golden_item(item: dict[str, object]) -> bool:
    return "question_bn" in item and "expected_module_id" not in item


def _is_golden_v2_item(item: dict[str, object]) -> bool:
    if "expected_module_id" not in item:
        return False
    return "question_en" in item or isinstance(item.get("question"), dict)


def _parse_golden_module_ids(raw: object, *, record_id: str) -> list[UUID]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Record {record_id}: module_id must be a list")
    return [UUID(str(module_id)) for module_id in raw]


def _parse_golden_source_card_ids(raw: object, *, record_id: str) -> tuple[UUID, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"Record {record_id}: source_card_id must be a list")
    return tuple(UUID(str(card_id)) for card_id in raw)


def _parse_golden_answerable(raw: object, *, module_ids: list[UUID]) -> tuple[bool, bool]:
    answerable = str(raw).strip().casefold() if raw is not None else "yes"
    if answerable == "no":
        return False, True
    if answerable == "partial":
        return True, False
    if not module_ids:
        return False, True
    return True, False


def _v2_question_texts(item: dict[str, object], idx: int) -> tuple[str, str]:
    question_data = item.get("question")
    if isinstance(question_data, dict):
        question_en = question_data.get("en")
        question_bn = question_data.get("bn")
    else:
        question_en = item.get("question_en")
        question_bn = item.get("question_bn")
    if not question_en or not question_bn:
        raise ValueError(f"Record {idx} must include question.en/bn or question_en/question_bn")
    return str(question_en), str(question_bn)


def _load_legacy_record(idx: int, item: dict[str, object]) -> GoldenRecord:
    question = item.get("question")
    if not question:
        raise ValueError(f"Record {idx} must include question")

    record_id = item.get("id") or f"q_{idx + 1:03d}"
    expected_module = item.get("expected_module")
    if expected_module is not None:
        expected_module = str(expected_module).strip() or None

    relevant_raw = item.get("relevant_module_ids")
    if relevant_raw is None:
        relevant_raw = []
    if not isinstance(relevant_raw, list):
        raise ValueError(f"Record {record_id}: relevant_module_ids must be a list")
    relevant_module_ids = [UUID(str(module_id)) for module_id in relevant_raw]

    category = str(item.get("category", ""))
    is_out_of_scope = _derive_is_out_of_scope(
        category=category,
        expected_module=expected_module,
        relevant_module_ids=relevant_module_ids,
        expected_module_id=None,
    )

    return GoldenRecord(
        id=str(record_id),
        category=category,
        question=str(question),
        expected_module=expected_module,
        relevant_module_ids=relevant_module_ids,
        is_answerable=bool(item.get("is_answerable", True)),
        is_out_of_scope=is_out_of_scope,
    )


def _load_v2_records(idx: int, item: dict[str, object]) -> list[GoldenRecord]:
    question_en, question_bn = _v2_question_texts(item, idx)

    category = str(item.get("question_category", ""))
    base_id = str(item.get("id") or f"q_{idx + 1:03d}")
    is_answerable = bool(item.get("is_answerable", True))

    raw_module_id = item.get("expected_module_id")
    if raw_module_id is None:
        expected_module_id = None
        relevant_module_ids: list[UUID] = []
        is_out_of_scope = True
    else:
        expected_module_id = UUID(str(raw_module_id))
        relevant_module_ids = [expected_module_id]
        is_out_of_scope = _derive_is_out_of_scope(
            category=category,
            expected_module=None,
            relevant_module_ids=relevant_module_ids,
            expected_module_id=expected_module_id,
        )

    records: list[GoldenRecord] = []
    for lang, question in (("en", str(question_en)), ("bn", str(question_bn))):
        suffix = "" if base_id.endswith(f"_{lang}") else f"_{lang}"
        records.append(
            GoldenRecord(
                id=f"{base_id}{suffix}",
                category=category,
                question=question,
                expected_module=None,
                relevant_module_ids=relevant_module_ids,
                is_answerable=is_answerable,
                expected_module_id=expected_module_id,
                question_lang=cast(QuestionLang, lang),
                is_out_of_scope=is_out_of_scope,
            )
        )
    return records


def _load_canonical_record(idx: int, item: dict[str, object]) -> GoldenRecord:
    question_bn = item.get("question_bn")
    if not question_bn:
        raise ValueError(f"Record {idx} must include question_bn")

    record_id = str(item.get("id") or f"q_{idx + 1:03d}")
    module_ids = _parse_golden_module_ids(item.get("module_id"), record_id=record_id)
    source_card_ids = _parse_golden_source_card_ids(item.get("source_card_id"), record_id=record_id)
    is_answerable, is_out_of_scope = _parse_golden_answerable(
        item.get("answerable"),
        module_ids=module_ids,
    )

    return GoldenRecord(
        id=record_id,
        category=str(item.get("query_type", "")),
        question=str(question_bn),
        expected_module=None,
        relevant_module_ids=module_ids,
        is_answerable=is_answerable,
        expected_module_id=module_ids[0] if len(module_ids) == 1 else None,
        expected_card_ids=source_card_ids,
        question_lang="bn",
        is_out_of_scope=is_out_of_scope,
    )


def _sanitize_json_control_chars_in_strings(text: str) -> str:
    """Replace raw newlines inside JSON string literals with spaces."""
    result: list[str] = []
    in_string = False
    escape = False
    for char in text:
        if escape:
            result.append(char)
            escape = False
            continue
        if char == "\\" and in_string:
            result.append(char)
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            result.append(char)
            continue
        if in_string and char in "\n\r":
            result.append(" ")
            continue
        result.append(char)
    return "".join(result)


def loads_golden_json(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_sanitize_json_control_chars_in_strings(text))


def load_golden_json_array(path: Path) -> list[object]:
    raw = loads_golden_json(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Golden dataset must be a JSON array: {path}")
    return raw


def load_golden_dataset(path: Path) -> list[GoldenRecord]:
    if not path.is_file():
        raise FileNotFoundError(f"Golden dataset not found: {path}")

    raw = load_golden_json_array(path)

    records: list[GoldenRecord] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Record {idx} must be an object")
        if _is_golden_v2_item(item):
            records.extend(_load_v2_records(idx, item))
        elif _is_canonical_golden_item(item):
            records.append(_load_canonical_record(idx, item))
        else:
            records.append(_load_legacy_record(idx, item))
    return records
