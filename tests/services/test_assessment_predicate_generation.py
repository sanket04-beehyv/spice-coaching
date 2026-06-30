"""Seed predicate consistency with assessment_topic_catalog."""

from __future__ import annotations

import json
from pathlib import Path

from platform_service.services.assessment_patient_match import (
    assessment_due_predicate,
    match_from_catalog_topic,
    match_to_dict,
)
from platform_service.services.assessment_topic_catalog import (
    assessment_due_trigger_code,
    canonical_assessment_topic_keys,
)
from platform_service.services.trigger_predicate_validator import validate_predicate_shape

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED_PATH = _REPO_ROOT / "seed" / "assessment_due_triggers.json"


def test_seed_predicates_match_catalog_generated_shape() -> None:
    payload = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    triggers = payload["triggers"]
    assert len(triggers) == len(canonical_assessment_topic_keys())

    by_topic = {entry["assessment_topic"]: entry for entry in triggers}
    assert set(by_topic) == set(canonical_assessment_topic_keys())

    for topic in canonical_assessment_topic_keys():
        entry = by_topic[topic]
        assert entry["trigger_code"] == assessment_due_trigger_code(topic)
        expected_predicate = assessment_due_predicate(topic)
        assert entry["predicate"] == expected_predicate
        expected_match = match_to_dict(match_from_catalog_topic(topic))
        assert entry["predicate"]["filter_predicate"]["match"] == expected_match
        validate_predicate_shape("workflow_event", entry["predicate"])
