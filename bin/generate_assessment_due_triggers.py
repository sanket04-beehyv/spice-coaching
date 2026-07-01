#!/usr/bin/env python3
"""Regenerate seed/assessment_due_triggers.json from assessment_topic_catalog."""

from __future__ import annotations

import json
from pathlib import Path

from platform_service.services.assessment_patient_match import assessment_due_predicate
from platform_service.services.assessment_topic_catalog import (
    assessment_due_trigger_code,
    canonical_assessment_topic_keys,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OUT = _REPO_ROOT / "seed" / "assessment_due_triggers.json"

_DESCRIPTIONS: dict[str, str] = {
    "anc": "Patients due for antenatal care visit",
    "maternal_health": "Maternal health follow-up due today",
    "pnc_mother": "Postnatal mother visit due today",
    "pnc_child": "Postnatal child visit due today",
    "pnc_neonate": "Neonatal postnatal visit due today",
    "childhood_visit": "Childhood wellness visit due today",
    "child_health": "Under-five child health visit due today",
    "neonatal": "Neonatal follow-up due today",
    "iccm": "ICCM community follow-up due today",
    "other_symptoms": "Other symptoms ICCM follow-up due today",
    "malaria": "Malaria follow-up due today",
    "severe_malaria": "Severe malaria follow-up due today",
    "pneumonia": "Pneumonia follow-up due today",
    "diarrhea": "Diarrhoea follow-up due today",
    "fever": "Fever follow-up due today",
    "cough": "Cough follow-up due today",
    "respiratory": "Respiratory illness follow-up due today",
    "jaundice": "Jaundice follow-up due today",
    "anemia": "Anemia follow-up due today",
    "hiv_aids": "HIV/AIDS follow-up due today",
    "severe_malnutrition": "Severe malnutrition follow-up due today",
    "moderate_malnutrition": "Moderate malnutrition follow-up due today",
    "ear_problem": "Ear problem follow-up due today",
    "under_two_months": "Under-two-months ICCM visit due today",
    "under_five_years": "Under-five-years ICCM visit due today",
    "worsened": "Worsened condition follow-up due today",
    "referred": "Referred patient follow-up due today",
    "on_treatment": "On-treatment household visit due today",
    "recovered": "Recovered patient follow-up due today",
    "general_danger_signs": "General danger signs follow-up due today",
    "muac": "MUAC malnutrition follow-up due today",
    "tb_symptoms": "TB symptoms follow-up due today",
    "symptoms": "Unspecified symptoms follow-up due today",
    "cbs": "CBS escalation follow-up due today",
    "miscarriage": "Miscarriage follow-up due today",
    "ncd": "NCD follow-up due today",
}


def main() -> None:
    triggers = []
    for topic in sorted(canonical_assessment_topic_keys()):
        triggers.append(
            {
                "trigger_code": assessment_due_trigger_code(topic),
                "assessment_topic": topic,
                "description": _DESCRIPTIONS.get(
                    topic, f"{topic.replace('_', ' ').title()} follow-up due today"
                ),
                "predicate": assessment_due_predicate(topic),
            }
        )
    payload = {
        "_comment": (
            "Global workflow_event triggers for morning assessment-due module matching. "
            "One row per canonical assessment_topic. Bound to module families at ingest "
            "time via trigger_binding_worker."
        ),
        "triggers": triggers,
    }
    _OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(triggers)} triggers to {_OUT}")


if __name__ == "__main__":
    main()
