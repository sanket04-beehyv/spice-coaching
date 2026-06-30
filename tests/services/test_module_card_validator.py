"""W-11 — module_card_validator tests (pure-unit)."""

from __future__ import annotations

from uuid import uuid4

from platform_service.services.module_card_validator import (
    ModuleCardValidator,
    annotate_field_flags,
    validate_cards_bulk,
    validate_quiz_questions_bulk,
)


def _prosemirror_body(text: str) -> dict:
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _block_list_body(text: str) -> list[dict]:
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def _card(**overrides) -> dict:
    base = {
        "card_family_id": str(uuid4()),
        "title": {"bn": "রক্তচাপ পরিমাপ"},
        "body": {"bn": "রোগীকে ৫ মিনিট বিশ্রাম দিতে হবে। সঠিক মাপের কাফ ব্যবহার করুন।"},
        "next_action": {"bn": "যদি রক্তচাপ ১৪০/৯০ এর বেশি হয় তবে রেফার করুন।"},
    }
    base.update(overrides)
    return base


def _quiz(**overrides) -> dict:
    base = {
        "question_family_id": str(uuid4()),
        "question": {"bn": "এই পরিস্থিতিতে আপনার পরবর্তী পদক্ষেপ কী হবে?"},
        "options": {
            "bn": [
                {"text": "তাকে বিশ্রাম নিতে বলুন"},
                {"text": "তাকে দ্রুত হাসপাতালে রেফার করুন"},
                {"text": "তাকে ব্যথানাশক ঔষধ দিন"},
                {"text": "তাকে পানি পান করতে বলুন"},
            ]
        },
        "correct_indices": [1],
    }
    base.update(overrides)
    return base


# ── Card: happy path ────────────────────────────────────────────────────


def test_clean_card_passes_with_no_warnings() -> None:
    res = ModuleCardValidator().validate_card(_card())
    assert res.is_valid is True
    assert res.hard_violations == []
    assert res.soft_warnings == []


def test_empty_body_bn_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_card(_card(body=""))
    assert res.is_valid is False
    assert any("body is empty" in v for v in res.hard_violations)


def test_prosemirror_body_bn_passes_when_nonempty() -> None:
    body = "রোগীকে ৫ মিনিট বিশ্রাম দিতে হবে। সঠিক মাপের কাফ ব্যবহার করুন।"
    res = ModuleCardValidator().validate_card(_card(body=_prosemirror_body(body)))
    assert res.is_valid is True


def test_empty_prosemirror_body_bn_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_card(_card(body={"type": "doc", "content": []}))
    assert res.is_valid is False
    assert any("body is empty" in v for v in res.hard_violations)


def test_block_list_body_bn_passes_when_nonempty() -> None:
    body = "রোগীকে ৫ মিনিট বিশ্রাম দিতে হবে। সঠিক মাপের কাফ ব্যবহার করুন।"
    res = ModuleCardValidator().validate_card(_card(body=_block_list_body(body)))
    assert res.is_valid is True


def test_empty_block_list_body_bn_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_card(_card(body=[]))
    assert res.is_valid is False
    assert any("body is empty" in v for v in res.hard_violations)


# ── Card: forbidden patterns (reused regex from v3.0 validator) ─────────


def test_dosage_mention_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_card(_card(body="রোগীকে ৫০০mg অ্যামক্সিসিলিন দিন।"))
    assert res.is_valid is False
    assert any("dosage" in v for v in res.hard_violations)


def test_drug_name_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_card(_card(body="amoxicillin দিতে হবে রোগীকে এই ক্ষেত্রে দ্রুত"))
    assert res.is_valid is False
    assert any("drug name" in v for v in res.hard_violations)


def test_diagnosis_language_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_card(
        _card(body={"bn": "You have malaria and you are diagnosed with severe anemia."})
    )
    assert res.is_valid is False
    assert any("diagnosis" in v for v in res.hard_violations)


# ── Card: Bangla bleed ──────────────────────────────────────────────────


def test_bangla_field_with_mostly_english_is_soft_warning() -> None:
    # Field is 100% English but lives in primary body — bleed-through.
    bleed = "Take rest for five minutes before measuring blood pressure carefully each time"
    res = ModuleCardValidator().validate_card(_card(body={"bn": bleed}))
    assert res.is_valid is True
    assert any("bleed-through" in w for w in res.soft_warnings)


def test_short_field_is_not_flagged_for_bleed() -> None:
    # Short fields might legitimately contain English (e.g. "BP")
    res = ModuleCardValidator().validate_card(_card(title={"bn": "BP measurement"}))
    assert not any("Latin" in w for w in res.soft_warnings)


def test_mixed_bangla_with_some_english_drug_units_passes() -> None:
    res = ModuleCardValidator().validate_card(_card(body="রক্তচাপ পরিমাপ করতে কাফ ব্যবহার করুন এবং BP রেকর্ড করুন।"))
    # English fragments < 25% Latin alphabetic chars → no warning
    assert not any("Latin" in w for w in res.soft_warnings)


# ── Card: length caps ──────────────────────────────────────────────────


def test_oversized_body_is_soft_warning() -> None:
    long_body = "চিকিৎসা পরামর্শ। " * 200  # well over 1200 chars
    res = ModuleCardValidator().validate_card(_card(body=long_body))
    assert res.is_valid is True
    assert any("body too long" in w for w in res.soft_warnings)


# ── Card: threshold consistency ────────────────────────────────────────


def test_body_number_inside_drift_tolerance_no_warning() -> None:
    res = ModuleCardValidator().validate_card(
        _card(
            body="যদি BP ১৪০ এর বেশি হয় তবে রেফার করুন",
            thresholds_jsonb={"bp_systolic_referral": 140},
        )
    )
    assert not any("threshold" in w for w in res.soft_warnings)


def test_body_number_disagrees_with_threshold_is_soft_warning() -> None:
    res = ModuleCardValidator().validate_card(
        _card(
            body="রক্তচাপ ১৩০ হলে রেফার করুন",
            thresholds_jsonb={"bp_systolic_referral": 140},
        )
    )
    assert any("threshold" in w for w in res.soft_warnings)


def test_body_number_in_different_order_of_magnitude_is_ignored() -> None:
    # 5 minutes vs threshold 140 mmHg — different OOM, not a contradiction
    res = ModuleCardValidator().validate_card(
        _card(
            body="৫ মিনিট অপেক্ষা করুন তারপর পরিমাপ করুন",
            thresholds_jsonb={"bp_systolic_referral": 140},
        )
    )
    assert not any("threshold" in w for w in res.soft_warnings)


# ── Quiz: happy path ───────────────────────────────────────────────────


def test_clean_quiz_passes() -> None:
    res = ModuleCardValidator().validate_quiz_question(_quiz())
    assert res.is_valid is True
    assert res.hard_violations == []


def test_empty_question_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_quiz_question(_quiz(question={"bn": ""}))
    assert res.is_valid is False
    assert any("question is empty" in v for v in res.hard_violations)


# ── Quiz: distinctness ─────────────────────────────────────────────────


def test_duplicate_options_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_quiz_question(
        _quiz(
            options={
                "bn": [
                    {"text": "তাকে রেফার করুন"},
                    {"text": "তাকে রেফার করুন"},  # exact duplicate
                    {"text": "ঔষধ দিন"},
                    {"text": "অপেক্ষা করুন"},
                ]
            }
        )
    )
    assert res.is_valid is False
    assert any("duplicates" in v for v in res.hard_violations)


def test_case_and_whitespace_insensitive_dedup() -> None:
    res = ModuleCardValidator().validate_quiz_question(
        _quiz(
            options={
                "bn": [
                    "রেফার করুন",
                    "  রেফার করুন  ",  # same after strip
                    "অপেক্ষা",
                    "ঔষধ",
                ]
            }
        )
    )
    assert res.is_valid is False
    assert any("duplicates" in v for v in res.hard_violations)


def test_too_few_options_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_quiz_question(_quiz(options={"bn": [{"text": "যা"}]}))
    assert res.is_valid is False
    assert any("at least 2 options" in v for v in res.hard_violations)


def test_empty_option_string_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_quiz_question(
        _quiz(options={"bn": [{"text": "প্রথম"}, {"text": ""}, {"text": "তৃতীয়"}, {"text": "চতুর্থ"}]})
    )
    assert res.is_valid is False
    assert any("option 1 is empty" in v for v in res.hard_violations)


def test_correct_index_out_of_range_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_quiz_question(_quiz(correct_indices=[5]))
    assert res.is_valid is False
    assert any("out-of-range" in v for v in res.hard_violations)


def test_empty_correct_indices_is_hard_violation() -> None:
    res = ModuleCardValidator().validate_quiz_question(_quiz(correct_indices=[]))
    assert res.is_valid is False
    assert any("correct_indices missing" in v for v in res.hard_violations)


# ── Bulk + flag annotation ─────────────────────────────────────────────


def test_validate_cards_bulk_returns_per_card_result() -> None:
    cards = [_card(), _card(body=""), _card()]
    out = validate_cards_bulk(cards)
    assert len(out) == 3
    assert [r.is_valid for r in out] == [True, False, True]


def test_validate_quiz_questions_bulk() -> None:
    qs = [_quiz(), _quiz(question={"bn": ""})]
    out = validate_quiz_questions_bulk(qs)
    assert [r.is_valid for r in out] == [True, False]


def test_annotate_field_flags_merges_warnings_and_violations() -> None:
    base = {"existing_flag": "keep"}
    res = ModuleCardValidator().validate_card(
        _card(body="", title={"bn": "some long enough title with English bleeding through here"})
    )
    out = annotate_field_flags(base, card_result=res)
    assert out["existing_flag"] == "keep"
    assert "validator_violations" in out  # body empty
