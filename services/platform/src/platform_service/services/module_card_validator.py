"""W-11 — quality validator for v3.3 ModuleCard + ModuleQuizQuestion.

Distinct from `services/card_validator.py` which validates the v3.0
`CoachingCardResponse` shape. This module operates on the v3.3 module-
pipeline shapes and adds 5 rules per Implementation Plan §11:

1. **Primary-script bleed** — primary-locale fields shouldn't contain >X%
   characters outside the deployment's native script (catches LLM forgetting
   language and outputting the wrong script in the primary locale slot).
2. **Threshold consistency** — when `thresholds_jsonb` declares a
   numerical cutoff, the body / next_action must not contradict it.
3. **Body length cap** — primary-locale body under a configured token cap so
   the card fits a mobile screen.
4. **Quiz options pairwise-distinct** — no two options identical (bug
   that makes the right answer obvious).
5. **Forbidden patterns** — reuses `_DOSAGE_RE`, `_DRUG_RE`,
   `_DIAGNOSIS_RE` from the v3.0 `card_validator.py` (CHWs shouldn't
   prescribe meds even from v3.3 modules).

Reference data:
- Settings.spice_referral_set — same source of truth as v3.0 validator.

Hard violations → caller should drop the card or fail the candidate.
Soft warnings → caller annotates `field_flags_jsonb` and the reviewer sees
them in the review surface (W-6).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from mc_foundation.locale import LOCALIZED_CARD_TEXT_FIELDS, get_script_range

from platform_service.config import Settings, get_settings
from platform_service.localized import primary_options, primary_text
from platform_service.services.card_body_text import (
    card_body_char_len,
    card_body_is_nonempty,
    card_body_plain_text,
    is_rich_text_body,
)

# Forbidden-expression regexes for clinical safety checks. Originally lived
# in the v3.0 card_validator (deleted in the architecture reset); kept here
# because the module-card validator is the only legitimate caller.
_DOSAGE_RE = re.compile(
    r"\b\d+(\.\d+)?\s*(mg|mcg|µg|ug|g\b|ml|cc|iu|units?|tablets?|capsules?|drops?|doses?|"
    r"teaspoon|tablespoon|sachet|ampoule|vial)\b",
    re.IGNORECASE,
)

_DRUG_RE = re.compile(
    r"\b("
    r"amoxicillin|amoxycillin|ampicillin|azithromycin|ciprofloxacin|"
    r"cotrimoxazole|trimethoprim|metronidazole|fluconazole|"
    r"chloroquine|artemisinin|artemether|lumefantrine|quinine|"
    r"doxycycline|tetracycline|erythromycin|"
    r"diazepam|phenobarbitone|phenobarbital|magnesium sulfate|"
    r"oxytocin|misoprostol|ergometrine|"
    r"gentamicin|penicillin|ceftriaxone|cloxacillin|"
    r"hydralazine|nifedipine|methyldopa|labetalol|"
    r"salbutamol|prednisolone|dexamethasone|"
    r"insulin|metformin|glibenclamide|"
    r"albendazole|mebendazole|ivermectin"
    r")\b",
    re.IGNORECASE,
)

_DIAGNOSIS_RE = re.compile(
    r"\b("
    r"you have|you are suffering from|you suffer from|"
    r"diagnosed with|diagnosis is|you are diagnosed|"
    r"test (result|shows?|confirms?)|positive for|"
    r"your condition is|you are positive|"
    r"you are infected with|you are affected by"
    r")\b",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


# ── Tunable thresholds ──────────────────────────────────────────────────
# Hard-coded for now; promote to settings if reviewers want them per-tenant.

# A primary-locale field can have *some* out-of-script characters (English drug
# names mid-prose, abbreviations like "BP", number units like "mg"). >25%
# means most of the field is the wrong script — that's bleed-through.
_PRIMARY_SCRIPT_BLEED_RATIO = 0.25
_PRIMARY_SCRIPT_BLEED_MIN_LENGTH = 30  # don't flag very short fields

# Approximation of "fits one mobile screen at 18sp body type".
_MAX_BODY_CHARS = 1200
_MAX_TITLE_CHARS = 120
_MAX_NEXT_ACTION_CHARS = 400

# Threshold drift tolerance: if thresholds_jsonb says 140 and body says 138,
# that's a real contradiction. ±2 is the slack we allow for rounding.
# Threshold drift band: only flag a body number as inconsistent if it's
# CLOSE-BUT-NOT-EQUAL to a threshold (off by 1–15). A body number that's
# very different (e.g. body says 90, threshold says 140) is almost always
# a different concept (diastolic vs systolic, days vs mg, etc.) — flagging
# it is a false positive. Off by 0 means it matches; off by >15 means
# different concept.
_THRESHOLD_DRIFT_MIN = 1
_THRESHOLD_DRIFT_MAX = 15


# ── Result containers ──────────────────────────────────────────────────


@dataclass(frozen=True)
class CardValidationResult:
    """Outcome of validating one ModuleCard."""

    card_family_id: str
    is_valid: bool
    hard_violations: list[str] = field(default_factory=list)
    soft_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QuizValidationResult:
    """Outcome of validating one quiz question."""

    question_family_id: str
    is_valid: bool
    hard_violations: list[str] = field(default_factory=list)
    soft_warnings: list[str] = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────


def _out_of_script_alpha_ratio(s: str, script_range: tuple[int, int] | None) -> float:
    """Fraction of *alpha* characters outside the deployment's native script."""
    if not s or script_range is None:
        return 0.0
    start, end = script_range
    alpha = [c for c in s if c.isalpha()]
    if not alpha:
        return 0.0
    out_of_script = sum(1 for c in alpha if not (start <= ord(c) <= end))
    return out_of_script / len(alpha)


def _primary_locale_raw(card: dict[str, Any], field: str, settings: Settings) -> Any:
    """Return the deployment-primary value for a localized card field."""
    raw = card.get(field)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return raw
    primary = settings.deployment_primary_locale
    if primary in raw:
        return raw.get(primary)
    if field == "body" and is_rich_text_body(raw):
        return raw
    return primary_text(raw, settings=settings)


def _primary_field_plain_text(card: dict[str, Any], field: str, settings: Settings) -> str:
    raw = _primary_locale_raw(card, field, settings)
    if field == "body":
        return card_body_plain_text(raw)
    if isinstance(raw, str):
        return raw.strip()
    return ""


_CARD_BLEED_FIELDS = LOCALIZED_CARD_TEXT_FIELDS
_QUIZ_BLEED_FIELDS = ("question", "explanation", "case_setup")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _extract_numbers(text: str) -> list[float]:
    """Pull every numeric literal out of text. Used for threshold consistency."""
    return [float(m.group()) for m in _NUMBER_RE.finditer(text or "")]


def _flatten_threshold_values(thresholds: dict[str, Any] | list[Any] | None) -> list[float]:
    """Walk an arbitrary thresholds_jsonb structure and collect every numeric
    leaf. Tolerant of the LLM's varying shapes (sometimes a list of dicts,
    sometimes a dict with mixed nested types)."""
    if thresholds is None:
        return []
    out: list[float] = []
    stack: list[Any] = [thresholds]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            out.append(float(node))
        elif isinstance(node, str):
            # Sometimes the LLM stores numbers as strings ("140")
            for n in _extract_numbers(node):
                out.append(n)
    return out


# ── Card validator ─────────────────────────────────────────────────────


class ModuleCardValidator:
    """Validates one ModuleCard's content. Stateless; create per-card or reuse."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def validate_card(self, card: dict[str, Any]) -> CardValidationResult:
        """Apply all rules to one card.

        `card` is a dict mirroring ModuleCard fields (matches the LLM-emitted
        shape and the SQLAlchemy model attributes). We accept dicts rather
        than ORM instances so the validator runs equally well on:
        - LLM output before persistence (Stage D)
        - reviewer-edit payload before update (W-6 PATCH)
        - DB rows (via card.__dict__)
        """
        hard: list[str] = []
        soft: list[str] = []

        cfid = str(card.get("card_family_id") or card.get("id") or "")

        title = _primary_field_plain_text(card, "title", self._settings)
        body = _primary_field_plain_text(card, "body", self._settings)
        prev = _primary_field_plain_text(card, "previous_practice", self._settings)
        curr = _primary_field_plain_text(card, "current_practice", self._settings)
        rationale = _primary_field_plain_text(card, "rationale_for_change", self._settings)

        # Required minimums. Refresher / digital_proficiency cards carry
        # body; content_update cards put their content in the practice
        # fields and leave body empty by design (W-5 §7).
        is_content_update_shape = bool(prev and curr and rationale)
        body_raw = _primary_locale_raw(card, "body", self._settings)
        if not card_body_is_nonempty(body_raw) and not is_content_update_shape:
            hard.append(
                "body is empty and content_update fields "
                "(previous_practice + current_practice + rationale_for_change) are not all populated"
            )

        # Primary-script bleed checks.
        script_range = get_script_range(self._settings.deployment_primary_locale)
        for fld in _CARD_BLEED_FIELDS:
            value = _primary_field_plain_text(card, fld, self._settings)
            if len(value) < _PRIMARY_SCRIPT_BLEED_MIN_LENGTH:
                continue
            if _out_of_script_alpha_ratio(value, script_range) > _PRIMARY_SCRIPT_BLEED_RATIO:
                soft.append(
                    f"{fld} contains >{int(_PRIMARY_SCRIPT_BLEED_RATIO * 100)}% "
                    f"Latin/out-of-script characters (likely language bleed-through)"
                )

        # Length caps (soft — these are layout hints, not safety issues).
        if len(title) > _MAX_TITLE_CHARS:
            soft.append(f"title too long ({len(title)} > {_MAX_TITLE_CHARS})")
        if card_body_char_len(body_raw) > _MAX_BODY_CHARS:
            soft.append(f"body too long ({card_body_char_len(body_raw)} > {_MAX_BODY_CHARS})")
        next_action = _primary_field_plain_text(card, "next_action", self._settings)
        if len(next_action) > _MAX_NEXT_ACTION_CHARS:
            soft.append(f"next_action too long ({len(next_action)} > {_MAX_NEXT_ACTION_CHARS})")

        # Forbidden patterns across all narrative fields (hard — safety).
        narrative_fields = (
            "title",
            "body",
            "next_action",
            "previous_practice",
            "current_practice",
            "rationale_for_change",
        )
        narrative_values: list[str] = []
        for fld in narrative_fields:
            raw = card.get(fld)
            if isinstance(raw, dict) and not is_rich_text_body(raw):
                narrative_values.extend(value for value in raw.values() if isinstance(value, str))
            else:
                narrative_values.append(_primary_field_plain_text(card, fld, self._settings))
        narrative = " ".join(value for value in narrative_values if value)
        if m := _DOSAGE_RE.search(narrative):
            hard.append(f"forbidden dosage expression: '{m.group()}'")
        if m := _DRUG_RE.search(narrative):
            hard.append(f"forbidden drug name: '{m.group()}'")
        if m := _DIAGNOSIS_RE.search(narrative):
            hard.append(f"forbidden diagnosis language: '{m.group()}'")

        # Threshold consistency: body/next_action numbers should match
        # thresholds_jsonb. This catches LLM drift like "refer at BP 130"
        # when thresholds say 140.
        thresholds = card.get("thresholds_jsonb") or card.get("thresholds")
        threshold_values = _flatten_threshold_values(thresholds)
        if threshold_values:
            body_numbers = _extract_numbers(body) + _extract_numbers(next_action)
            # Flag only body numbers that are a "near miss" of a threshold —
            # off by 1–15. Equal means it matches; off by more than 15 is
            # almost certainly a different concept (e.g. diastolic vs
            # systolic, minutes vs mmHg) and would produce false positives.
            for bn in body_numbers:
                for tv in threshold_values:
                    diff = abs(bn - tv)
                    if _THRESHOLD_DRIFT_MIN <= diff <= _THRESHOLD_DRIFT_MAX:
                        soft.append(
                            f"body mentions number {bn:g} that's close to "
                            f"threshold {tv:g} but doesn't match (off by {diff:g})"
                        )
                        break  # one warning per body number is enough

        # Referral destination check — only when the card carries an EXPLICIT
        # referral_destination field (not a v3.3 ModuleCard concept by default,
        # but tolerated in case Stage D ever adds it). v3.3 modules embed
        # referral guidance inside next_action as primary-locale prose, so we
        # don't try to parse facility names out of it.
        referral = card.get("referral_destination")
        if isinstance(referral, str) and referral:
            if len(referral) <= 80 and referral not in self._settings.spice_referral_set:
                if not any(d in referral for d in self._settings.spice_referral_set):
                    soft.append(f"referral destination '{referral[:60]}' not in SPICE register")

        return CardValidationResult(
            card_family_id=cfid,
            is_valid=len(hard) == 0,
            hard_violations=hard,
            soft_warnings=soft,
        )

    def validate_quiz_question(self, q: dict[str, Any]) -> QuizValidationResult:
        """Apply quiz-specific rules to one question."""
        hard: list[str] = []
        soft: list[str] = []

        qfid = str(q.get("question_family_id") or q.get("id") or "")

        question = (primary_text(q.get("question"), settings=self._settings) or "").strip()
        if not question:
            hard.append("question is empty")

        # Primary-script bleed on question text + explanation.
        script_range = get_script_range(self._settings.deployment_primary_locale)
        for fld in _QUIZ_BLEED_FIELDS:
            value = (primary_text(q.get(fld), settings=self._settings) or "").strip()
            if len(value) < _PRIMARY_SCRIPT_BLEED_MIN_LENGTH:
                continue
            if _out_of_script_alpha_ratio(value, script_range) > _PRIMARY_SCRIPT_BLEED_RATIO:
                soft.append(
                    f"{fld} contains >{int(_PRIMARY_SCRIPT_BLEED_RATIO * 100)}% "
                    f"out-of-script characters (likely language bleed-through)"
                )

        # Options validation.
        options = primary_options(q.get("options"), settings=self._settings) or []
        if not isinstance(options, list) or len(options) < 2:
            hard.append("quiz must have at least 2 options")
        else:
            # Pairwise distinctness (case-insensitive, whitespace-tolerant).
            normalised = []
            for opt in options:
                if isinstance(opt, dict):
                    text = str(opt.get("text") or "").strip().lower()
                else:
                    text = str(opt).strip().lower()
                normalised.append(text)
            seen: dict[str, int] = {}
            for idx, text in enumerate(normalised):
                if not text:
                    hard.append(f"option {idx} is empty")
                    continue
                if text in seen:
                    hard.append(
                        f"options {seen[text]} and {idx} are duplicates (makes correct answer obvious)"
                    )
                else:
                    seen[text] = idx

        # Correct indices in range.
        correct_indices = q.get("correct_indices") or []
        if not isinstance(correct_indices, list) or not correct_indices:
            hard.append("correct_indices missing or empty")
        elif isinstance(options, list):
            for ci in correct_indices:
                if not isinstance(ci, int) or ci < 0 or ci >= len(options):
                    hard.append(f"correct_indices contains out-of-range value {ci!r}")
                    break

        return QuizValidationResult(
            question_family_id=qfid,
            is_valid=len(hard) == 0,
            hard_violations=hard,
            soft_warnings=soft,
        )


# ── Convenience: bulk validate ─────────────────────────────────────────


def validate_cards_bulk(
    cards: list[dict[str, Any]], settings: Settings | None = None
) -> list[CardValidationResult]:
    v = ModuleCardValidator(settings=settings)
    return [v.validate_card(c) for c in cards]


def validate_quiz_questions_bulk(
    questions: list[dict[str, Any]], settings: Settings | None = None
) -> list[QuizValidationResult]:
    v = ModuleCardValidator(settings=settings)
    return [v.validate_quiz_question(q) for q in questions]


def annotate_field_flags(
    field_flags_jsonb: dict[str, Any] | None,
    *,
    card_result: CardValidationResult | None = None,
    quiz_result: QuizValidationResult | None = None,
) -> dict[str, Any]:
    """Merge validator output into a card's or question's `field_flags_jsonb`.
    Reviewer-surface (W-6) reads this to surface warnings on the edit screen."""
    base = dict(field_flags_jsonb or {})
    if card_result is not None:
        if card_result.soft_warnings:
            base["validator_warnings"] = list(card_result.soft_warnings)
        if card_result.hard_violations:
            base["validator_violations"] = list(card_result.hard_violations)
    if quiz_result is not None:
        if quiz_result.soft_warnings:
            base["validator_warnings"] = list(quiz_result.soft_warnings)
        if quiz_result.hard_violations:
            base["validator_violations"] = list(quiz_result.hard_violations)
    return base
