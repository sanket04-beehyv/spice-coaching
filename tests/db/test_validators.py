"""W-1 unit tests — application-layer constraint validators.

Pure-Python tests of the validators in platform_service.db.validators.
No DB needed.
"""

from uuid import uuid4

import pytest
from platform_service.db.validators import (
    ValidationError,
    validate_module_card_content_completeness,
    validate_module_card_membership_referent,
    validate_module_review_aspects_complete,
    validate_trigger_predicate,
)

# ── Module card content completeness ────────────────────────────────────


class TestModuleCardContentCompleteness:
    def test_refresher_with_body_bn_passes(self) -> None:
        validate_module_card_content_completeness({"body_bn": "কিছু বাংলা"}, "refresher")

    def test_refresher_without_body_bn_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_module_card_content_completeness({"body_en": "english only"}, "refresher")
        assert exc_info.value.code == "card_required_fields_missing"

    def test_refresher_with_empty_body_bn_fails(self) -> None:
        with pytest.raises(ValidationError):
            validate_module_card_content_completeness({"body_bn": "   "}, "refresher")

    def test_digital_proficiency_with_body_bn_passes(self) -> None:
        validate_module_card_content_completeness({"body_bn": "x"}, "digital_proficiency")

    def test_content_update_with_all_three_fields_passes(self) -> None:
        validate_module_card_content_completeness(
            {
                "previous_practice_bn": "old",
                "current_practice_bn": "new",
                "rationale_for_change_bn": "why",
            },
            "content_update",
        )

    def test_content_update_missing_previous_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_module_card_content_completeness(
                {
                    "current_practice_bn": "new",
                    "rationale_for_change_bn": "why",
                },
                "content_update",
            )
        assert exc_info.value.code == "card_required_fields_missing"
        assert "previous_practice_bn" in exc_info.value.message

    def test_content_update_missing_rationale_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_module_card_content_completeness(
                {
                    "previous_practice_bn": "old",
                    "current_practice_bn": "new",
                },
                "content_update",
            )
        assert "rationale_for_change_bn" in exc_info.value.message

    def test_unknown_module_type_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_module_card_content_completeness({"body_bn": "x"}, "weird_type")
        assert exc_info.value.code == "unknown_module_type"


# ── Trigger predicate validation ────────────────────────────────────────


class TestTriggerPredicate:
    def test_unknown_trigger_kind_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_trigger_predicate("not_a_kind", {})
        assert exc_info.value.code == "unknown_trigger_kind"

    # gap kind --------------------------------------------------------

    def test_gap_with_required_field_passes(self) -> None:
        validate_trigger_predicate("gap", {"behavioural_gap_id": str(uuid4())})

    def test_gap_with_full_predicate_passes(self) -> None:
        validate_trigger_predicate(
            "gap",
            {
                "behavioural_gap_id": str(uuid4()),
                "occurrence_count_threshold": 2,
                "window_days": 14,
            },
        )

    def test_gap_missing_gap_id_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_trigger_predicate("gap", {"occurrence_count_threshold": 2})
        assert exc_info.value.code == "gap_predicate_missing_gap_id"

    def test_gap_zero_threshold_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_trigger_predicate(
                "gap",
                {"behavioural_gap_id": str(uuid4()), "occurrence_count_threshold": 0},
            )
        assert exc_info.value.code == "gap_predicate_invalid_field"

    def test_gap_negative_window_fails(self) -> None:
        with pytest.raises(ValidationError):
            validate_trigger_predicate(
                "gap",
                {"behavioural_gap_id": str(uuid4()), "window_days": -1},
            )

    def test_gap_string_threshold_fails(self) -> None:
        with pytest.raises(ValidationError):
            validate_trigger_predicate(
                "gap",
                {"behavioural_gap_id": str(uuid4()), "occurrence_count_threshold": "2"},
            )

    # workflow_event kind ---------------------------------------------

    def test_workflow_event_with_event_code_passes(self) -> None:
        validate_trigger_predicate("workflow_event", {"spice_event_code": "assessment_submitted"})

    def test_workflow_event_with_payload_filters_passes(self) -> None:
        validate_trigger_predicate(
            "workflow_event",
            {
                "spice_event_code": "risk_flag_observed",
                "payload_filters": {"risk_type": "high_bp"},
            },
        )

    def test_workflow_event_missing_event_code_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_trigger_predicate("workflow_event", {})
        assert exc_info.value.code == "workflow_event_predicate_missing_event_code"

    def test_workflow_event_empty_event_code_fails(self) -> None:
        with pytest.raises(ValidationError):
            validate_trigger_predicate("workflow_event", {"spice_event_code": "  "})

    def test_workflow_event_invalid_payload_filters_fails(self) -> None:
        with pytest.raises(ValidationError):
            validate_trigger_predicate(
                "workflow_event",
                {"spice_event_code": "x", "payload_filters": "not a dict"},
            )

    # content_push kind -----------------------------------------------

    def test_content_push_with_audience_filter_passes(self) -> None:
        validate_trigger_predicate(
            "content_push",
            {"audience_filter": {"tenant_id": [str(uuid4())]}},
        )

    def test_content_push_with_urgent_passes(self) -> None:
        validate_trigger_predicate(
            "content_push",
            {"audience_filter": {}, "urgency": "urgent"},
        )

    def test_content_push_missing_audience_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_trigger_predicate("content_push", {})
        assert exc_info.value.code == "content_push_predicate_missing_audience"

    def test_content_push_invalid_audience_type_fails(self) -> None:
        with pytest.raises(ValidationError):
            validate_trigger_predicate("content_push", {"audience_filter": "not a dict"})

    def test_content_push_invalid_urgency_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_trigger_predicate(
                "content_push",
                {"audience_filter": {}, "urgency": "RIGHT_NOW"},
            )
        assert exc_info.value.code == "content_push_predicate_invalid_urgency"


# ── Membership family consistency ───────────────────────────────────────


class TestMembershipReferent:
    def test_matching_family_passes(self) -> None:
        family = uuid4()
        validate_module_card_membership_referent(family, family)

    def test_mismatched_family_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_module_card_membership_referent(uuid4(), uuid4())
        assert exc_info.value.code == "membership_family_mismatch"


# ── Review aspects ──────────────────────────────────────────────────────


class TestReviewAspects:
    def test_all_required_aspects_true_passes(self) -> None:
        validate_module_review_aspects_complete(
            {
                "clinical_correctness": True,
                "bangla_content": True,
                "source_provenance": True,
            }
        )

    def test_extra_aspect_passes(self) -> None:
        validate_module_review_aspects_complete(
            {
                "clinical_correctness": True,
                "bangla_content": True,
                "source_provenance": True,
                "tts_rendering": True,
            }
        )

    def test_none_or_empty_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_module_review_aspects_complete(None)
        assert exc_info.value.code == "review_aspects_missing"

        with pytest.raises(ValidationError):
            validate_module_review_aspects_complete({})

    def test_one_aspect_missing_fails(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            validate_module_review_aspects_complete({"clinical_correctness": True, "bangla_content": True})
        assert exc_info.value.code == "review_aspects_incomplete"
        assert "source_provenance" in exc_info.value.message

    def test_aspect_set_to_false_fails(self) -> None:
        with pytest.raises(ValidationError):
            validate_module_review_aspects_complete(
                {
                    "clinical_correctness": True,
                    "bangla_content": False,
                    "source_provenance": True,
                }
            )
