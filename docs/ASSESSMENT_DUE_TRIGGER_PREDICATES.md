# Assessment-due trigger predicates

Assessment-due workflow triggers (`trigger_kind=workflow_event`, `trigger_code=wf:assessment_due:{topic}`) are synced to the SPICE Android client via `GET /sync/triggers`. Each row includes a `predicate_jsonb` object the device evaluates against due follow-up rows from `POST /follow-up/list` (primary) or due patients from `POST /patient/list` (fallback).

## Predicate shape

```json
{
  "spice_event_code": "assessment_due",
  "filter_predicate": {
    "assessment_topic": "malaria",
    "match": {
      "encounter_type_any": ["MALARIA"],
      "reason_any": ["MALARIA"],
      "reason_display_any": ["Malaria", "Severe Malaria", "Uncomplicated Malaria"],
      "diagnosis_any": ["MALARIA", "SEVERE_MALARIA", "SEVEREMALARIA"],
      "patient_status_any": ["malaria"],
      "appointment_type_any": ["HH_VISIT", "REFERRED", "MEDICAL_REVIEW"],
      "encounter_program_any": ["ICCM"],
      "encounter_name_any": [],
      "is_pregnant": null,
      "max_age": null,
      "min_age": null
    }
  }
}
```

Canonical definitions live in [`seed/assessment_due_triggers.json`](../seed/assessment_due_triggers.json). Server logic that generates and validates them is in [`assessment_patient_match.py`](../services/platform/src/platform_service/services/assessment_patient_match.py).

## SPICE field mapping

| `match` evaluation | FollowUpDTO (`/follow-up/list`) | PatientDTO (`/patient/list`) | Platform snapshot |
|--------------------|---------------------------------|------------------------------|-------------------|
| `encounter_type_any` | — | `type` (assessment encounter) | `encounter_type` |
| `encounter_program_any` | `encounterType` | — | `encounter_program` |
| `encounter_name_any` | `encounterName` | — | `encounter_name` |
| `appointment_type_any` | `type` (HH_VISIT / REFERRED / MEDICAL_REVIEW) | — | `appointment_type` |
| `reason_display_any` | `reason` (comma-separated display names) | — | `reason` |
| `reason_any` | — | `reason` (enum token) | `reason` |
| `diagnosis_any` | — | `diagnosisType[]` | `diagnosis_types` |
| `patient_status_any` | `patientStatus` | `patientStatus` | `patient_status` |
| `is_pregnant` | — | `isPregnant` | `is_pregnant` |
| `max_age` / `min_age` | `age` | `age` | `age` |

Token fields (`encounter_type_any`, `reason_any`, `diagnosis_any`) are compared case-insensitively after uppercasing and normalizing spaces to underscores. `reason_display_any` splits `reason` on commas and compares trimmed segments case-insensitively.

## Match semantics (OR)

A due patient or follow-up row matches the trigger when **any** clause is true:

1. `encounter_type` is listed in `encounter_type_any`, or normalizes to `filter_predicate.assessment_topic`
2. Any comma-separated `reason` segment is listed in `reason_display_any`
3. `reason` is listed in `reason_any`, or normalizes to `assessment_topic`
4. Any diagnosis is listed in `diagnosis_any`, or normalizes to `assessment_topic`
5. Normalized `patient_status` equals `assessment_topic`, or is listed in `patient_status_any`
6. `encounter_name` is listed in `encounter_name_any`
7. `encounterType` / program is listed in `encounter_program_any`
8. `appointment_type` is listed in `appointment_type_any`
9. `is_pregnant` is `true` and the patient `is_pregnant == true`
10. `max_age` is set and `age <= max_age`
11. `min_age` is set and `age >= min_age`

Empty lists and `null` demographic bounds do not contribute a clause.

## Device sync

1. Call `GET /sync/triggers?since=...` (see [README.md](../README.md)).
2. Persist `TriggerDefinitionSyncPayload.predicate_jsonb` locally.
3. For each due follow-up row, evaluate predicates where `spice_event_code == "assessment_due"`.
4. Use `module_trigger_binding` rows (synced in the same bundle) to resolve coaching modules for fired trigger codes.

Module-to-trigger associations created at ingest are module-level bindings in `module_trigger_binding`; they appear in the triggers sync bundle (not on `ModuleSyncPayload`). Each binding references `module_id` (a specific published module version).

## Server parity

Morning assessment-due cards are deferred. Predicate generation and validation use [`assessment_patient_match.py`](../services/platform/src/platform_service/services/assessment_patient_match.py) (`assessment_due_predicate`, `patient_matches_filter`).

## Regenerating seed JSON

```bash
uv run python bin/generate_assessment_due_triggers.py
```
