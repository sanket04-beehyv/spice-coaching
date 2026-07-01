"""Assessment patient-match predicate tests."""

from __future__ import annotations

from dataclasses import dataclass

from platform_service.services.assessment_patient_match import (
    assessment_due_predicate,
    match_from_catalog_topic,
    match_to_dict,
    patient_matches_filter,
)
from platform_service.services.assessment_topic_catalog import (
    normalize_topic_key,
    topics_for_demographics,
    topics_for_diagnosis,
    topics_for_display_reason,
    topics_for_encounter_type,
    topics_for_reason,
)


@dataclass(frozen=True)
class _Patient:
    patient_id: str
    member_id: str
    gender: str | None
    age: int | None
    is_pregnant: bool | None
    patient_status: str | None
    diagnosis_types: tuple[str, ...]
    encounter_type: str | None
    encounter_name: str | None = None
    encounter_program: str | None = None
    reason: str | None = None
    appointment_type: str | None = None


def _legacy_topics_for_patient(patient: _Patient) -> set[str]:
    topics: set[str] = set()
    topics.update(topics_for_encounter_type(patient.encounter_type))
    topics.update(topics_for_reason(patient.reason))
    for segment in (patient.reason or "").split(","):
        topics.update(topics_for_display_reason(segment))
    for diagnosis in patient.diagnosis_types:
        topics.update(topics_for_diagnosis(diagnosis))
    topics.update(
        topics_for_demographics(
            gender=patient.gender,
            age=patient.age,
            is_pregnant=patient.is_pregnant,
        )
    )
    if patient.is_pregnant:
        topics.add("maternal_health")
    status = normalize_topic_key(patient.patient_status or "")
    if status:
        topics.add(status)
    return {normalize_topic_key(topic) for topic in topics if normalize_topic_key(topic)}


def test_anc_predicate_matches_encounter_and_pregnancy() -> None:
    predicate = assessment_due_predicate("anc")
    filter_predicate = predicate["filter_predicate"]
    pregnant_anc = _Patient(
        patient_id="p1",
        member_id="m1",
        gender="female",
        age=28,
        is_pregnant=True,
        patient_status="active",
        diagnosis_types=(),
        encounter_type="ANC",
        reason=None,
    )
    assert patient_matches_filter(pregnant_anc, filter_predicate)

    diagnosis_only = _Patient(
        patient_id="p2",
        member_id="m2",
        gender="female",
        age=30,
        is_pregnant=True,
        patient_status=None,
        diagnosis_types=("ANC",),
        encounter_type=None,
        reason=None,
    )
    assert patient_matches_filter(diagnosis_only, filter_predicate)


def test_malaria_predicate_matches_encounter_reason_and_diagnosis() -> None:
    predicate = assessment_due_predicate("malaria")
    filter_predicate = predicate["filter_predicate"]
    patient = _Patient(
        patient_id="p3",
        member_id="m3",
        gender="male",
        age=4,
        is_pregnant=False,
        patient_status=None,
        diagnosis_types=("SEVERE_MALARIA",),
        encounter_type="MALARIA",
        reason="MALARIA",
    )
    assert patient_matches_filter(patient, filter_predicate)


def test_malaria_predicate_matches_follow_up_display_reason() -> None:
    predicate = assessment_due_predicate("malaria")
    filter_predicate = predicate["filter_predicate"]
    follow_up = _Patient(
        patient_id="p3b",
        member_id="m3b",
        gender="male",
        age=4,
        is_pregnant=False,
        patient_status="OnTreatment",
        diagnosis_types=(),
        encounter_type=None,
        encounter_program="ICCM",
        reason="Malaria, Fever",
        appointment_type="HH_VISIT",
    )
    assert patient_matches_filter(follow_up, filter_predicate)


def test_general_danger_signs_matches_display_reason() -> None:
    predicate = assessment_due_predicate("general_danger_signs")
    follow_up = _Patient(
        patient_id="p-gds",
        member_id="m-gds",
        gender="female",
        age=2,
        is_pregnant=False,
        patient_status="OnTreatment",
        diagnosis_types=(),
        encounter_type=None,
        encounter_program="ICCM",
        reason="General Danger Signs",
        appointment_type="HH_VISIT",
    )
    assert patient_matches_filter(follow_up, predicate["filter_predicate"])


def test_child_health_predicate_matches_age_and_encounter() -> None:
    predicate = assessment_due_predicate("child_health")
    filter_predicate = predicate["filter_predicate"]
    patient = _Patient(
        patient_id="p4",
        member_id="m4",
        gender="male",
        age=3,
        is_pregnant=False,
        patient_status=None,
        diagnosis_types=(),
        encounter_type=None,
        reason="PNEUMONIA",
    )
    assert patient_matches_filter(patient, filter_predicate)


def test_match_from_catalog_topic_serializes_all_required_fields() -> None:
    match = match_from_catalog_topic("worsened")
    payload = match_to_dict(match)
    assert payload["diagnosis_any"] == ["WORSENED"]
    assert payload["patient_status_any"] == ["worsened"]
