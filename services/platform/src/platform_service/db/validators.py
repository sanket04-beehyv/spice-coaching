"""v3.3 application-layer constraint validators.

Cross-table constraints that don't fit into a simple SQL CHECK or UNIQUE.
Called at write time by repositories and services to keep the data model
invariants intact.

Per Implementation Plan v2 §3 / Data Model v3.3 §11.
"""

from typing import Any
from uuid import UUID

from platform_service.config import get_settings
from platform_service.localized import primary_text
from platform_service.services.card_body_text import card_body_is_nonempty


class ValidationError(ValueError):
    """Raised when an application-layer invariant is violated."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ── Module card content completeness (Data Model v3.3 §5.3 constraint) ───
# Refresher / digital_proficiency cards need primary-locale body populated.
# content_update cards need previous_practice AND current_practice AND
# rationale_for_change populated in the deployment primary locale.

_REQUIRED_FIELDS_BY_MODULE_TYPE: dict[str, tuple[str, ...]] = {
    "refresher": ("body",),
    "digital_proficiency": ("body",),
    "content_update": ("previous_practice", "current_practice", "rationale_for_change"),
}


def _primary_locale_field_value(card_dict: dict[str, Any], field: str) -> Any:
    """Return the deployment-primary locale value for a localized card field."""
    raw = card_dict.get(field)
    if raw is None:
        return None
    settings = get_settings()
    primary = settings.deployment_primary_locale
    if isinstance(raw, dict) and primary in raw:
        return raw.get(primary)
    return raw


def _field_has_primary_content(card_dict: dict[str, Any], field: str) -> bool:
    raw = card_dict.get(field)
    if field == "body":
        return card_body_is_nonempty(_primary_locale_field_value(card_dict, field))
    if isinstance(raw, dict):
        return bool((primary_text(raw) or "").strip())
    if isinstance(raw, str):
        return bool(raw.strip())
    return bool(raw)


def validate_module_card_content_completeness(card_dict: dict[str, Any], module_type: str) -> None:
    """Reject a card whose required-by-module-type fields are missing or empty.

    `card_dict` is the card payload as a plain dict (post-Pydantic-validation,
    pre-DB-insert). `module_type` is the parent module's `module_type`.
    """
    required = _REQUIRED_FIELDS_BY_MODULE_TYPE.get(module_type)
    if required is None:
        raise ValidationError(
            "unknown_module_type",
            f"module_type='{module_type}' is not one of {sorted(_REQUIRED_FIELDS_BY_MODULE_TYPE)}",
        )
    missing = [f for f in required if not _field_has_primary_content(card_dict, f)]
    if missing:
        raise ValidationError(
            "card_required_fields_missing",
            f"module_type='{module_type}' requires non-empty {missing}; "
            "card content_completeness validator rejected this insert",
        )


# ── Trigger predicate validation (Data Model v3.3 §6.2) ─────────────────
# Per-kind JSON Schema. Implementation kept lightweight (manual key checks)
# to avoid pulling jsonschema as a runtime dependency for one validator;
# escalation to jsonschema is fine when predicate shapes grow.

_VALID_TRIGGER_KINDS: tuple[str, ...] = ("gap", "workflow_event", "content_push")


def validate_trigger_predicate(trigger_kind: str, predicate: dict[str, Any]) -> None:
    """Reject a trigger_definition with a malformed predicate for its kind."""
    if trigger_kind not in _VALID_TRIGGER_KINDS:
        raise ValidationError(
            "unknown_trigger_kind",
            f"trigger_kind='{trigger_kind}' is not one of {list(_VALID_TRIGGER_KINDS)}",
        )

    if trigger_kind == "gap":
        if "behavioural_gap_id" not in predicate:
            raise ValidationError(
                "gap_predicate_missing_gap_id",
                "trigger_kind='gap' predicate requires 'behavioural_gap_id'",
            )
        # Optional fields: occurrence_count_threshold (int >0), window_days (int >0).
        for key in ("occurrence_count_threshold", "window_days"):
            if key in predicate:
                value = predicate[key]
                if not isinstance(value, int) or value <= 0:
                    raise ValidationError(
                        "gap_predicate_invalid_field",
                        f"'{key}' must be a positive int; got {value!r}",
                    )

    elif trigger_kind == "workflow_event":
        if "spice_event_code" not in predicate:
            raise ValidationError(
                "workflow_event_predicate_missing_event_code",
                "trigger_kind='workflow_event' predicate requires 'spice_event_code'",
            )
        if not isinstance(predicate["spice_event_code"], str) or not predicate["spice_event_code"].strip():
            raise ValidationError(
                "workflow_event_predicate_invalid_event_code",
                "'spice_event_code' must be a non-empty string",
            )
        # Optional payload_filters dict.
        payload_filters = predicate.get("payload_filters")
        if payload_filters is not None and not isinstance(payload_filters, dict):
            raise ValidationError(
                "workflow_event_predicate_invalid_payload_filters",
                "'payload_filters' must be a dict if present",
            )

    elif trigger_kind == "content_push":
        audience_filter = predicate.get("audience_filter")
        if audience_filter is None:
            raise ValidationError(
                "content_push_predicate_missing_audience",
                "trigger_kind='content_push' predicate requires 'audience_filter'",
            )
        if not isinstance(audience_filter, dict):
            raise ValidationError(
                "content_push_predicate_invalid_audience",
                "'audience_filter' must be a dict",
            )
        # Optional urgency: "normal" | "urgent"
        urgency = predicate.get("urgency", "normal")
        if urgency not in ("normal", "urgent"):
            raise ValidationError(
                "content_push_predicate_invalid_urgency",
                f"'urgency' must be 'normal' or 'urgent'; got {urgency!r}",
            )


# ── Module membership consistency (Data Model v3.3 §5.6a/b) ─────────────
# Every membership row's referenced card/quiz must resolve to a versioned row
# whose family_id matches the family the membership claims. This guards
# against orphaned or family-mismatched memberships.


def validate_module_card_membership_referent(
    membership_card_family_id: UUID,
    referenced_card_family_id: UUID,
) -> None:
    """Reject a membership whose claimed card_family doesn't match the referenced card row's family."""
    if membership_card_family_id != referenced_card_family_id:
        raise ValidationError(
            "membership_family_mismatch",
            f"membership claims card_family_id={membership_card_family_id} but the referenced "
            f"module_card row has card_family_id={referenced_card_family_id}",
        )


# ── Module review approval gate ─────────────────────────────────────────
# Approval requires all expected review_aspects to be True.

_REQUIRED_REVIEW_ASPECTS: tuple[str, ...] = (
    "clinical_correctness",
    "primary_language_content",
    "source_provenance",
)


def validate_module_review_aspects_complete(review_aspects: dict[str, Any] | None) -> None:
    """Reject an approval when the reviewer hasn't attested to all required aspects.

    `tts_rendering` is required only when the module has TTS enabled (caller
    passes that aspect when applicable). The base required set is the three
    above.
    """
    if not review_aspects:
        raise ValidationError(
            "review_aspects_missing",
            "module approval requires `review_aspects` to be set",
        )
    missing = [a for a in _REQUIRED_REVIEW_ASPECTS if not review_aspects.get(a)]
    if missing:
        raise ValidationError(
            "review_aspects_incomplete",
            f"reviewer must attest to {missing} before approving the module",
        )
