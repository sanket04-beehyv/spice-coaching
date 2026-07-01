"""Topic-key catalog for community assessment-due morning card matching."""

from __future__ import annotations

import re

CHILD_HEALTH_MAX_AGE = 5
NEONATAL_MAX_AGE = 0
_CHILD_HEALTH_MAX_AGE = CHILD_HEALTH_MAX_AGE
_NEONATAL_MAX_AGE = NEONATAL_MAX_AGE

_ASSESSMENT_DUE_TRIGGER_PREFIX = "wf:assessment_due:"

_COMMUNITY_APPOINTMENT_TYPES = ("HH_VISIT", "REFERRED", "MEDICAL_REVIEW")

_ENCOUNTER_TYPE_TOPICS: dict[str, tuple[str, ...]] = {
    "ANC": ("anc", "maternal_health"),
    "PNC_MOTHER": ("pnc_mother", "maternal_health"),
    "PNC_CHILD": ("pnc_child", "child_health"),
    "PNC_NEONATE": ("pnc_neonate", "neonatal", "child_health"),
    "CHILDHOOD_VISIT": ("childhood_visit", "child_health"),
    "ICCM": ("iccm",),
    "OTHER_SYMPTOMS": ("other_symptoms", "iccm"),
    "MALARIA": ("malaria", "iccm"),
    "PNEUMONIA": ("pneumonia", "iccm", "respiratory"),
    "DIARRHEA": ("diarrhea", "iccm"),
    "UNDER_TWO_MONTHS": ("under_two_months", "iccm", "child_health", "neonatal"),
    "UNDER_FIVE_YEARS": ("under_five_years", "iccm", "child_health"),
}

_REASON_TOPICS: dict[str, tuple[str, ...]] = {
    "MALARIA": ("malaria", "iccm"),
    "PNEUMONIA": ("pneumonia", "iccm", "respiratory"),
    "DIARRHEA": ("diarrhea", "iccm"),
    "DIARRHOEA": ("diarrhea", "iccm"),
    "FEVER": ("fever", "iccm"),
    "COUGH": ("pneumonia", "respiratory", "iccm", "cough"),
    "MUAC": ("muac", "iccm"),
    "SYMPTOMS": ("symptoms", "other_symptoms", "iccm"),
}

_DIAGNOSIS_ALIASES: dict[str, tuple[str, ...]] = {
    "MALARIA": ("malaria",),
    "PNEUMONIA": ("pneumonia", "respiratory"),
    "DIARRHEA": ("diarrhea",),
    "DIARRHOEA": ("diarrhea",),
    "ANC": ("anc", "maternal_health"),
    "PNC": ("pnc_mother", "maternal_health"),
    "PNC_NEONATE": ("pnc_neonate", "neonatal", "child_health"),
    "OTHER_SYMPTOMS": ("other_symptoms", "iccm"),
    "UNDER_TWO_MONTHS": ("under_two_months", "iccm", "child_health", "neonatal"),
    "UNDER_FIVE_YEARS": ("under_five_years", "iccm", "child_health"),
    "SEVEREMALARIA": ("severe_malaria", "malaria", "iccm"),
    "SEVERE_MALARIA": ("severe_malaria", "malaria", "iccm"),
    "UNCOMPLICATEDMALARIA": ("malaria", "iccm"),
    "UNCOMPLICATED_MALARIA": ("malaria", "iccm"),
    "JAUNDICE": ("jaundice", "iccm"),
    "HIVINFECTION": ("hiv_aids", "iccm"),
    "HIV_AIDS": ("hiv_aids", "iccm"),
    "HIVAIDS": ("hiv_aids", "iccm"),
    "ANEMIA": ("anemia", "iccm"),
    "SEVEREMALNUTRITION": ("severe_malnutrition", "iccm"),
    "SEVERE_MALNUTRITION": ("severe_malnutrition", "iccm"),
    "MODERATEMALNUTRITION": ("moderate_malnutrition", "iccm"),
    "MODERATE_MALNUTRITION": ("moderate_malnutrition", "iccm"),
    "EARPROBLEM": ("ear_problem", "iccm"),
    "EAR_PROBLEM": ("ear_problem", "iccm"),
    "WORSENED": ("worsened",),
    "MUAC": ("muac", "iccm"),
}

# FollowUpDTO.reason display strings (ReferralReasons.aliasOf + disease_category seeds).
_REASON_DISPLAY_ALIASES: dict[str, tuple[str, ...]] = {
    "general_danger_signs": ("General Danger Signs",),
    "fever": ("Fever",),
    "pneumonia": ("Pneumonia", "Pneumonia / Fever", "Cough or Difficult Breathing"),
    "malaria": ("Malaria", "Uncomplicated Malaria"),
    "severe_malaria": ("Severe Malaria",),
    "symptoms": ("Symptoms",),
    "other_symptoms": ("Symptoms", "TB Symptoms"),
    "tb_symptoms": ("TB Symptoms",),
    "diarrhea": ("Diarrhoea", "Watery diarrhoea / Dysentery", "Dysentry (Bloody Diarrhoea)"),
    "muac": ("MUAC",),
    "anc": ("ANC Signs",),
    "maternal_health": ("ANC Signs", "PNC Mother Signs", "High Risk Mother", "Gaps in PNC"),
    "pnc_mother": ("PNC Mother Signs", "PNC Visit", "Gaps in PNC"),
    "pnc_child": ("Childhood Visit Signs",),
    "childhood_visit": ("Childhood Visit Signs",),
    "pnc_neonate": ("PNC Neonate Signs",),
    "child_health": ("Childhood Visit Signs", "PNC Neonate Signs"),
    "cough": ("Cough",),
    "miscarriage": ("Miscarriage",),
    "ncd": ("NCD", "NCDSymptoms"),
    "cbs": ("CBS",),
    "jaundice": ("Jaundice",),
    "anemia": ("Anemia",),
    "hiv_aids": ("HIV/AIDS", "HIV Infection"),
    "severe_malnutrition": ("Severe Malnutrition",),
    "moderate_malnutrition": ("Moderate Malnutrition",),
    "ear_problem": ("Ear Problem",),
    "worsened": ("Worsened",),
    "referred": ("Referred",),
    "on_treatment": ("OnTreatment", "On Treatment"),
    "recovered": ("Recovered",),
}

_ENCOUNTER_NAME_TOPICS: dict[str, tuple[str, ...]] = {
    "ANC": ("anc", "maternal_health"),
    "PNC_MOTHER": ("pnc_mother", "maternal_health"),
    "PNC_CHILD": ("pnc_child", "child_health"),
    "PNC_NEONATE": ("pnc_neonate", "neonatal", "child_health"),
    "CHILDHOOD_VISIT": ("childhood_visit", "child_health"),
}

_ENCOUNTER_PROGRAM_TOPICS: dict[str, tuple[str, ...]] = {
    "ICCM": ("iccm", "malaria", "pneumonia", "diarrhea", "fever", "other_symptoms"),
    "RMNCH": ("anc", "maternal_health", "pnc_mother", "pnc_neonate", "childhood_visit"),
    "CHILDHOOD_VISIT": ("childhood_visit", "child_health"),
}

# Topics that match any community appointment type when due for follow-up.
_DEFAULT_APPOINTMENT_TOPICS = frozenset(
    {
        "anc",
        "maternal_health",
        "pnc_mother",
        "pnc_child",
        "pnc_neonate",
        "childhood_visit",
        "child_health",
        "neonatal",
        "iccm",
        "other_symptoms",
        "malaria",
        "severe_malaria",
        "pneumonia",
        "diarrhea",
        "fever",
        "cough",
        "respiratory",
        "jaundice",
        "anemia",
        "hiv_aids",
        "severe_malnutrition",
        "moderate_malnutrition",
        "ear_problem",
        "under_two_months",
        "under_five_years",
        "worsened",
        "general_danger_signs",
        "muac",
        "tb_symptoms",
        "symptoms",
        "cbs",
        "miscarriage",
    }
)

_APPOINTMENT_TYPE_TOPICS: dict[str, tuple[str, ...]] = {
    "REFERRED": ("referred",),
    "HH_VISIT": ("on_treatment",),
    "MEDICAL_REVIEW": ("on_treatment",),
}

_CANONICAL_ASSESSMENT_TOPIC_KEYS: frozenset[str] = frozenset(
    {
        "anc",
        "maternal_health",
        "pnc_mother",
        "pnc_child",
        "pnc_neonate",
        "childhood_visit",
        "child_health",
        "neonatal",
        "iccm",
        "other_symptoms",
        "malaria",
        "severe_malaria",
        "pneumonia",
        "diarrhea",
        "fever",
        "cough",
        "respiratory",
        "jaundice",
        "anemia",
        "hiv_aids",
        "severe_malnutrition",
        "moderate_malnutrition",
        "ear_problem",
        "under_two_months",
        "under_five_years",
        "worsened",
        "referred",
        "on_treatment",
        "recovered",
        "general_danger_signs",
        "muac",
        "tb_symptoms",
        "symptoms",
        "cbs",
        "miscarriage",
        "ncd",
    }
)

# ReferralReasons.aliasOf display strings for exhaustiveness tests.
REFERRAL_REASON_DISPLAY_NAMES: frozenset[str] = frozenset(
    {
        "General Danger Signs",
        "Fever",
        "Pneumonia",
        "Malaria",
        "Symptoms",
        "Diarrhoea",
        "MUAC",
        "ANC Signs",
        "PNC Mother Signs",
        "Childhood Visit Signs",
        "PNC Neonate Signs",
        "Cough",
        "Miscarriage",
        "NCD",
        "NCDSymptoms",
        "TB Symptoms",
        "CBS",
    }
)

FOLLOW_UP_CRITERIA_BUCKETS: frozenset[str] = frozenset(
    {
        "malaria",
        "pneumonia",
        "diarrhea",
        "muac",
        "escalation",
        "referral",
        "ancVisit",
        "pncVisit",
        "childVisit",
    }
)


def normalize_topic_key(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    return re.sub(r"[^a-z0-9_]", "", text)


def assessment_due_trigger_code(topic_key: str) -> str:
    """Stable trigger_code for a canonical assessment-due workflow trigger."""
    return f"{_ASSESSMENT_DUE_TRIGGER_PREFIX}{normalize_topic_key(topic_key)}"


def canonical_assessment_topic_keys() -> frozenset[str]:
    """Allowed assessment_topic values for classification and trigger seeding."""
    return _CANONICAL_ASSESSMENT_TOPIC_KEYS


def encounter_type_topic_map() -> dict[str, tuple[str, ...]]:
    return _ENCOUNTER_TYPE_TOPICS


def reason_topic_map() -> dict[str, tuple[str, ...]]:
    return _REASON_TOPICS


def diagnosis_topic_map() -> dict[str, tuple[str, ...]]:
    return _DIAGNOSIS_ALIASES


def reason_display_aliases_map() -> dict[str, tuple[str, ...]]:
    return _REASON_DISPLAY_ALIASES


def encounter_name_topic_map() -> dict[str, tuple[str, ...]]:
    return _ENCOUNTER_NAME_TOPICS


def encounter_program_topic_map() -> dict[str, tuple[str, ...]]:
    return _ENCOUNTER_PROGRAM_TOPICS


def appointment_type_topic_map() -> dict[str, tuple[str, ...]]:
    return _APPOINTMENT_TYPE_TOPICS


def is_canonical_assessment_topic(topic_key: str) -> bool:
    return normalize_topic_key(topic_key) in _CANONICAL_ASSESSMENT_TOPIC_KEYS


def related_topic_keys(topic_key: str) -> frozenset[str]:
    """Parent/alias tags used for search_metadata overlap."""
    key = normalize_topic_key(topic_key)
    related: set[str] = {key}
    for mapping in (
        _ENCOUNTER_TYPE_TOPICS,
        _REASON_TOPICS,
        _DIAGNOSIS_ALIASES,
        _REASON_DISPLAY_ALIASES,
        _ENCOUNTER_NAME_TOPICS,
        _ENCOUNTER_PROGRAM_TOPICS,
        _APPOINTMENT_TYPE_TOPICS,
    ):
        for source, targets in mapping.items():
            normalized_source = normalize_topic_key(source)
            normalized_targets = {normalize_topic_key(t) for t in targets}
            if key == normalized_source or key in normalized_targets:
                related.add(normalized_source)
                related.update(normalized_targets)
    return frozenset(related)


def topics_for_encounter_type(encounter_type: str | None) -> tuple[str, ...]:
    if not encounter_type:
        return ()
    direct = _ENCOUNTER_TYPE_TOPICS.get(encounter_type.strip().upper())
    if direct:
        return direct
    return (normalize_topic_key(encounter_type),)


def topics_for_reason(reason: str | None) -> tuple[str, ...]:
    if not reason:
        return ()
    direct = _REASON_TOPICS.get(reason.strip().upper())
    if direct:
        return direct
    return (normalize_topic_key(reason),)


def topics_for_diagnosis(diagnosis: str | None) -> tuple[str, ...]:
    if not diagnosis:
        return ()
    direct = _DIAGNOSIS_ALIASES.get(diagnosis.strip().upper())
    if direct:
        return direct
    return (normalize_topic_key(diagnosis),)


def topics_for_demographics(
    *,
    gender: str | None,
    age: int | None,
    is_pregnant: bool | None,
) -> tuple[str, ...]:
    del gender
    topics: list[str] = []
    if is_pregnant:
        topics.extend(["anc", "maternal_health"])
    if age is not None:
        if age <= _NEONATAL_MAX_AGE:
            topics.extend(["neonatal", "child_health", "under_two_months"])
        elif age <= _CHILD_HEALTH_MAX_AGE:
            topics.extend(["child_health", "under_five_years"])
    return tuple(dict.fromkeys(topics))


def topics_for_display_reason(reason: str | None) -> tuple[str, ...]:
    """Map a FollowUpDTO.reason segment to canonical topics."""
    if not reason or not reason.strip():
        return ()
    segment = reason.strip()
    matched: list[str] = []
    segment_lower = segment.lower()
    for topic, aliases in _REASON_DISPLAY_ALIASES.items():
        for alias in aliases:
            if alias.lower() == segment_lower:
                matched.append(topic)
                break
    if not matched:
        matched.append(normalize_topic_key(segment))
    return tuple(dict.fromkeys(matched))


def topics_for_follow_up_criteria_bucket(bucket: str) -> tuple[str, ...]:
    """Map FollowUpCriteria bucket keys to canonical topics."""
    key = bucket.strip()
    mapping: dict[str, tuple[str, ...]] = {
        "malaria": ("malaria", "severe_malaria"),
        "pneumonia": ("pneumonia", "respiratory", "cough"),
        "diarrhea": ("diarrhea",),
        "muac": ("muac", "severe_malnutrition", "moderate_malnutrition"),
        "escalation": ("general_danger_signs", "cbs", "worsened"),
        "referral": ("referred",),
        "ancVisit": ("anc", "maternal_health"),
        "pncVisit": ("pnc_mother", "maternal_health"),
        "childVisit": ("childhood_visit", "child_health"),
    }
    return mapping.get(key, (normalize_topic_key(key),))


def reason_display_aliases_for_topic(topic_key: str) -> tuple[str, ...]:
    topic = normalize_topic_key(topic_key)
    return _REASON_DISPLAY_ALIASES.get(topic, ())


def appointment_types_for_topic(topic_key: str) -> tuple[str, ...]:
    topic = normalize_topic_key(topic_key)
    if topic in _DEFAULT_APPOINTMENT_TOPICS:
        return _COMMUNITY_APPOINTMENT_TYPES
    types: set[str] = set()
    for appt_type, targets in _APPOINTMENT_TYPE_TOPICS.items():
        if topic in {normalize_topic_key(t) for t in targets}:
            types.add(appt_type)
    return tuple(sorted(types))
