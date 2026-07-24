"""Unit tests for golden dataset resolution helpers."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from eval.rag.corpus import CardCorpusDoc, CorpusDoc
from eval.rag.dataset import (
    GoldenRecord,
    collect_golden_resolution_issues,
    load_golden_dataset,
    unresolvable_golden_record_ids,
)

_MODULE_ID = UUID("4422af94-662c-4a84-9c54-68dc4cf6888d")
_MISSING_MODULE_ID = UUID("4423af94-662c-4a84-9c54-68dc4cf6888d")
_CARD_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_MISSING_CARD_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _corpus_docs() -> list[CorpusDoc]:
    return [
        CorpusDoc(
            module_id=_MODULE_ID,
            primary_title="Hypertension Management",
            title_en="Hypertension Management",
            title_bn=None,
            text="hypertension management",
        )
    ]


def _cards_by_module() -> dict[UUID, list[CardCorpusDoc]]:
    return {
        _MODULE_ID: [
            CardCorpusDoc(
                module_id=_MODULE_ID,
                card_id=_CARD_ID,
                card_index=0,
                card_family_id=None,
                primary_title="Counseling on Lifestyle Modifications",
                title_en="Counseling on Lifestyle Modifications",
                title_bn=None,
                text="lifestyle counseling",
            )
        ]
    }


def test_legacy_unresolved_module_title_emits_issue() -> None:
    record = GoldenRecord(
        id="legacy_001",
        category="factual",
        question="What is hypertension?",
        expected_module="Missing Module Title",
        relevant_module_ids=[],
        is_answerable=True,
    )
    issues = collect_golden_resolution_issues([record], _corpus_docs(), _cards_by_module())
    assert len(issues) == 1
    assert issues[0].record_id == "legacy_001"
    assert "no published module titled" in issues[0].message
    assert unresolvable_golden_record_ids(issues) == {"legacy_001"}


def test_missing_card_id_emits_issue() -> None:
    record = GoldenRecord(
        id="q_007_en",
        category="Hypertension Management",
        question="How does diet impact hypertension?",
        expected_module=None,
        relevant_module_ids=[_MODULE_ID],
        is_answerable=True,
        expected_module_id=_MODULE_ID,
        expected_card_ids=(_MISSING_CARD_ID,),
        question_lang="en",
    )
    issues = collect_golden_resolution_issues([record], _corpus_docs(), _cards_by_module())
    assert len(issues) == 1
    assert issues[0].record_id == "q_007_en"
    assert "expected_card_id" in issues[0].message
    assert str(_MISSING_CARD_ID) in issues[0].message


def test_out_of_scope_record_has_no_issues() -> None:
    record = GoldenRecord(
        id="oos_001",
        category="out-of-scope",
        question="What is the weather?",
        expected_module=None,
        relevant_module_ids=[],
        is_answerable=True,
        is_out_of_scope=True,
    )
    issues = collect_golden_resolution_issues([record], _corpus_docs(), _cards_by_module())
    assert issues == []
    assert unresolvable_golden_record_ids(issues) == set()


def test_fully_resolvable_record_has_no_issues() -> None:
    record = GoldenRecord(
        id="q_001_en",
        category="Hypertension Management",
        question="What lifestyle changes help manage hypertension?",
        expected_module=None,
        relevant_module_ids=[_MODULE_ID],
        is_answerable=True,
        expected_module_id=_MODULE_ID,
        expected_card_ids=(_CARD_ID,),
        question_lang="en",
    )
    issues = collect_golden_resolution_issues([record], _corpus_docs(), _cards_by_module())
    assert issues == []


def test_v2_loads_localized_question_dict(tmp_path: Path) -> None:
    module_id = str(_MODULE_ID)
    dataset = tmp_path / "localized_question.json"
    dataset.write_text(
        f"""
        [
          {{
            "id": "q_001",
            "question": {{"en": "What is hypertension?", "bn": "উচ্চ রক্তচাপ কী?"}},
            "expected_module_id": "{module_id}",
            "question_category": "Hypertension Management"
          }}
        ]
        """,
        encoding="utf-8",
    )
    records = load_golden_dataset(dataset)
    assert len(records) == 2
    by_lang = {record.question_lang: record for record in records}
    assert by_lang["en"].question == "What is hypertension?"
    assert by_lang["bn"].question == "উচ্চ রক্তচাপ কী?"
    assert by_lang["en"].expected_module_id == _MODULE_ID


def test_canonical_loads_single_bn_row(tmp_path: Path) -> None:
    module_id = str(_MODULE_ID)
    card_id = str(_CARD_ID)
    dataset = tmp_path / "canonical.json"
    dataset.write_text(
        f"""
        [
          {{
            "id": "Q001",
            "question_bn": "উচ্চ রক্তচাপ কী?",
            "expected_answer_bn": "উচ্চ রক্তচাপ হলো রক্তচাপ বেশি থাকা।",
            "source_card_id": ["{card_id}"],
            "query_type": "Factual",
            "answerable": "yes",
            "module_id": ["{module_id}"]
          }}
        ]
        """,
        encoding="utf-8",
    )
    records = load_golden_dataset(dataset)
    assert len(records) == 1
    record = records[0]
    assert record.id == "Q001"
    assert record.question_lang == "bn"
    assert record.expected_module_id == _MODULE_ID
    assert record.expected_card_ids == (_CARD_ID,)


def test_multi_card_resolution_fails_when_any_card_missing() -> None:
    record = GoldenRecord(
        id="Q241",
        category="Situational",
        question="test",
        expected_module=None,
        relevant_module_ids=[_MODULE_ID],
        is_answerable=True,
        expected_module_id=_MODULE_ID,
        expected_card_ids=(_MISSING_CARD_ID, _CARD_ID),
        question_lang="bn",
    )
    issues = collect_golden_resolution_issues([record], _corpus_docs(), _cards_by_module())
    assert len(issues) == 1
    assert str(_MISSING_CARD_ID) in issues[0].message
