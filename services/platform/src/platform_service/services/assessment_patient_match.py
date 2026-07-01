"""Flat patient-match predicates for assessment-due workflow triggers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from platform_service.services.assessment_topic_catalog import (
    CHILD_HEALTH_MAX_AGE,
    NEONATAL_MAX_AGE,
    appointment_types_for_topic,
    diagnosis_topic_map,
    encounter_name_topic_map,
    encounter_program_topic_map,
    encounter_type_topic_map,
    normalize_topic_key,
    reason_display_aliases_for_topic,
    reason_topic_map,
)

_ASSESSMENT_DUE_EVENT = "assessment_due"

_MATCH_LIST_FIELDS = (
    "encounter_type_any",
    "reason_any",
    "diagnosis_any",
    "patient_status_any",
    "reason_display_any",
    "appointment_type_any",
    "encounter_name_any",
    "encounter_program_any",
)
_MATCH_DEMOGRAPHIC_FIELDS = ("is_pregnant", "max_age", "min_age")
_MATCH_REQUIRED_FIELDS = _MATCH_LIST_FIELDS + _MATCH_DEMOGRAPHIC_FIELDS
MATCH_REQUIRED_FIELDS = _MATCH_REQUIRED_FIELDS

_PREGNANCY_TOPICS = frozenset({"anc", "maternal_health"})
_AGE_MAX_TOPICS: dict[str, int] = {
    "neonatal": NEONATAL_MAX_AGE,
    "child_health": CHILD_HEALTH_MAX_AGE,
}


class PatientMatchInput(Protocol):
    encounter_type: str | None
    encounter_name: str | None
    encounter_program: str | None
    reason: str | None
    patient_status: str | None
    appointment_type: str | None
    is_pregnant: bool | None
    age: int | None
    diagnosis_types: tuple[str, ...]


def normalize_spice_token(value: str) -> str:
    """Normalize a SPICE encounter/reason/diagnosis token for list comparison."""
    return value.strip().upper().replace(" ", "_")


def normalize_display_token(value: str) -> str:
    return value.strip().lower()


def split_reason_segments(reason: str | None) -> tuple[str, ...]:
    if not reason:
        return ()
    return tuple(part.strip() for part in reason.split(",") if part.strip())


@dataclass(frozen=True)
class AssessmentPatientMatch:
    encounter_type_any: tuple[str, ...]
    reason_any: tuple[str, ...]
    diagnosis_any: tuple[str, ...]
    patient_status_any: tuple[str, ...]
    reason_display_any: tuple[str, ...]
    appointment_type_any: tuple[str, ...]
    encounter_name_any: tuple[str, ...]
    encounter_program_any: tuple[str, ...]
    is_pregnant: bool | None
    max_age: int | None
    min_age: int | None


def _sorted_tokens(tokens: set[str]) -> tuple[str, ...]:
    return tuple(sorted(tokens))


def match_from_catalog_topic(topic_key: str) -> AssessmentPatientMatch:
    """Invert assessment_topic_catalog maps into a flat OR-match block."""
    topic = normalize_topic_key(topic_key)
    encounters: set[str] = set()
    reasons: set[str] = set()
    diagnoses: set[str] = set()
    encounter_names: set[str] = set()
    encounter_programs: set[str] = set()

    for encounter_type, targets in encounter_type_topic_map().items():
        if topic in {normalize_topic_key(t) for t in targets}:
            encounters.add(encounter_type)

    for reason, targets in reason_topic_map().items():
        if topic in {normalize_topic_key(t) for t in targets}:
            reasons.add(reason)

    for diagnosis, targets in diagnosis_topic_map().items():
        if topic in {normalize_topic_key(t) for t in targets}:
            diagnoses.add(diagnosis)

    for encounter_name, targets in encounter_name_topic_map().items():
        if topic in {normalize_topic_key(t) for t in targets}:
            encounter_names.add(encounter_name)

    for program, targets in encounter_program_topic_map().items():
        if topic in {normalize_topic_key(t) for t in targets}:
            encounter_programs.add(program)

    reason_displays = set(reason_display_aliases_for_topic(topic))
    appointment_types = set(appointment_types_for_topic(topic))

    is_pregnant = True if topic in _PREGNANCY_TOPICS else None
    max_age = _AGE_MAX_TOPICS.get(topic)
    min_age = None

    return AssessmentPatientMatch(
        encounter_type_any=_sorted_tokens(encounters),
        reason_any=_sorted_tokens(reasons),
        diagnosis_any=_sorted_tokens(diagnoses),
        patient_status_any=(topic,),
        reason_display_any=_sorted_tokens(reason_displays),
        appointment_type_any=_sorted_tokens(appointment_types),
        encounter_name_any=_sorted_tokens(encounter_names),
        encounter_program_any=_sorted_tokens(encounter_programs),
        is_pregnant=is_pregnant,
        max_age=max_age,
        min_age=min_age,
    )


def match_to_dict(match: AssessmentPatientMatch) -> dict[str, Any]:
    return {
        "encounter_type_any": list(match.encounter_type_any),
        "reason_any": list(match.reason_any),
        "diagnosis_any": list(match.diagnosis_any),
        "patient_status_any": list(match.patient_status_any),
        "reason_display_any": list(match.reason_display_any),
        "appointment_type_any": list(match.appointment_type_any),
        "encounter_name_any": list(match.encounter_name_any),
        "encounter_program_any": list(match.encounter_program_any),
        "is_pregnant": match.is_pregnant,
        "max_age": match.max_age,
        "min_age": match.min_age,
    }


def assessment_due_predicate(topic_key: str) -> dict[str, Any]:
    topic = normalize_topic_key(topic_key)
    match = match_from_catalog_topic(topic)
    return {
        "spice_event_code": _ASSESSMENT_DUE_EVENT,
        "filter_predicate": {
            "assessment_topic": topic,
            "match": match_to_dict(match),
        },
    }


def _token_in_list(value: str | None, allowed: tuple[str, ...]) -> bool:
    if not value or not allowed:
        return False
    normalized = normalize_spice_token(value)
    return normalized in {normalize_spice_token(item) for item in allowed}


def _display_in_list(value: str | None, allowed: tuple[str, ...]) -> bool:
    if not value or not allowed:
        return False
    allowed_normalized = {normalize_display_token(item) for item in allowed}
    for segment in split_reason_segments(value):
        if normalize_display_token(segment) in allowed_normalized:
            return True
    return normalize_display_token(value) in allowed_normalized


def patient_matches_match(
    patient: PatientMatchInput,
    match: dict[str, Any],
    *,
    assessment_topic: str,
) -> bool:
    """Return True when any OR clause in the flat match block fires."""
    topic = normalize_topic_key(assessment_topic)

    encounter_types = match.get("encounter_type_any")
    if isinstance(encounter_types, list) and encounter_types:
        if _token_in_list(patient.encounter_type, tuple(encounter_types)):
            return True
        if patient.encounter_type and normalize_topic_key(patient.encounter_type) == topic:
            return True

    reasons = match.get("reason_any")
    if isinstance(reasons, list) and reasons:
        if _token_in_list(patient.reason, tuple(reasons)):
            return True
        for segment in split_reason_segments(patient.reason):
            if _token_in_list(segment, tuple(reasons)):
                return True
            if normalize_topic_key(segment) == topic:
                return True

    reason_displays = match.get("reason_display_any")
    if isinstance(reason_displays, list) and reason_displays:
        if _display_in_list(patient.reason, tuple(reason_displays)):
            return True

    diagnoses = match.get("diagnosis_any")
    if isinstance(diagnoses, list) and diagnoses:
        for diagnosis in patient.diagnosis_types:
            if _token_in_list(diagnosis, tuple(diagnoses)):
                return True
            if normalize_topic_key(diagnosis) == topic:
                return True

    statuses = match.get("patient_status_any")
    if patient.patient_status:
        normalized_status = normalize_topic_key(patient.patient_status)
        if normalized_status == topic:
            return True
        if isinstance(statuses, list) and statuses:
            if normalized_status in {normalize_topic_key(item) for item in statuses}:
                return True

    encounter_names = match.get("encounter_name_any")
    if isinstance(encounter_names, list) and encounter_names:
        if _token_in_list(patient.encounter_name, tuple(encounter_names)):
            return True

    encounter_programs = match.get("encounter_program_any")
    if isinstance(encounter_programs, list) and encounter_programs:
        program = patient.encounter_program or patient.encounter_type
        if _token_in_list(program, tuple(encounter_programs)):
            return True

    appointment_types = match.get("appointment_type_any")
    if isinstance(appointment_types, list) and appointment_types:
        if _token_in_list(patient.appointment_type, tuple(appointment_types)):
            return True

    is_pregnant = match.get("is_pregnant")
    if is_pregnant is True and patient.is_pregnant is True:
        return True

    max_age = match.get("max_age")
    if max_age is not None and patient.age is not None and patient.age <= int(max_age):
        return True

    min_age = match.get("min_age")
    if min_age is not None and patient.age is not None and patient.age >= int(min_age):
        return True

    return False


def patient_matches_filter(patient: PatientMatchInput, filter_predicate: dict[str, Any]) -> bool:
    assessment_topic = filter_predicate.get("assessment_topic")
    if not isinstance(assessment_topic, str) or not assessment_topic.strip():
        return False
    match = filter_predicate.get("match")
    if not isinstance(match, dict):
        return False
    return patient_matches_match(
        patient,
        match,
        assessment_topic=assessment_topic,
    )


__all__ = (
    "AssessmentPatientMatch",
    "MATCH_REQUIRED_FIELDS",
    "PatientMatchInput",
    "assessment_due_predicate",
    "match_from_catalog_topic",
    "match_to_dict",
    "normalize_display_token",
    "normalize_spice_token",
    "patient_matches_filter",
    "patient_matches_match",
    "split_reason_segments",
)
