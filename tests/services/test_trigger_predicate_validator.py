"""W-8 — trigger_predicate_validator: shape + reference validation."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module_family import ModuleFamily
from platform_service.services.trigger_predicate_validator import (
    PredicateError,
    validate_predicate,
    validate_predicate_references,
    validate_predicate_shape,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

# ── Pure-unit shape validation ──────────────────────────────────────────


def test_shape_unknown_kind_rejected() -> None:
    with pytest.raises(PredicateError) as exc:
        validate_predicate_shape("not-a-kind", {})
    assert exc.value.code == "unknown_trigger_kind"


def test_shape_predicate_must_be_object() -> None:
    with pytest.raises(PredicateError) as exc:
        validate_predicate_shape("gap", "string")  # type: ignore[arg-type]
    assert exc.value.code == "predicate_not_object"


def test_shape_gap_kind_missing_required_field() -> None:
    with pytest.raises(PredicateError) as exc:
        validate_predicate_shape("gap", {})
    assert exc.value.code == "missing_required_field"
    assert exc.value.field == "behavioural_gap_code"


def test_shape_gap_kind_minimal_valid() -> None:
    # Only the required field — defaults fill in everything else.
    validate_predicate_shape("gap", {"behavioural_gap_code": "missed_referral"})


def test_shape_gap_kind_with_all_fields() -> None:
    validate_predicate_shape(
        "gap",
        {
            "behavioural_gap_code": "missed_referral",
            "occurrences_threshold": 3,
            "window_days": 7,
            "severity_floor": "moderate",
        },
    )


def test_shape_gap_kind_unknown_field_rejected() -> None:
    with pytest.raises(PredicateError) as exc:
        validate_predicate_shape(
            "gap",
            {"behavioural_gap_code": "x", "frequency_threshhold": 2},  # typo
        )
    assert exc.value.code == "unknown_field"
    assert exc.value.field == "frequency_threshhold"


def test_shape_gap_kind_negative_threshold_rejected() -> None:
    with pytest.raises(PredicateError) as exc:
        validate_predicate_shape("gap", {"behavioural_gap_code": "x", "occurrences_threshold": 0})
    assert exc.value.code == "value_constraint_failed"
    assert exc.value.field == "occurrences_threshold"


def test_shape_gap_kind_bool_rejected_for_int_field() -> None:
    with pytest.raises(PredicateError) as exc:
        validate_predicate_shape("gap", {"behavioural_gap_code": "x", "occurrences_threshold": True})
    assert exc.value.code == "wrong_type"


def test_shape_gap_kind_invalid_severity_rejected() -> None:
    with pytest.raises(PredicateError) as exc:
        validate_predicate_shape("gap", {"behavioural_gap_code": "x", "severity_floor": "critical"})
    assert exc.value.code == "enum_violation"


def test_shape_workflow_event_minimal_valid() -> None:
    validate_predicate_shape("workflow_event", {"spice_event_code": "assessment_submitted"})


def test_shape_workflow_event_missing_code() -> None:
    with pytest.raises(PredicateError) as exc:
        validate_predicate_shape("workflow_event", {})
    assert exc.value.code == "missing_required_field"
    assert exc.value.field == "spice_event_code"


def test_shape_workflow_event_with_filter() -> None:
    validate_predicate_shape(
        "workflow_event",
        {
            "spice_event_code": "assessment_submitted",
            "filter_predicate": {"outcome": "referred"},
        },
    )


def test_shape_content_push_minimal_valid() -> None:
    validate_predicate_shape("content_push", {"module_family_id": str(uuid4())})


def test_shape_content_push_missing_id() -> None:
    with pytest.raises(PredicateError) as exc:
        validate_predicate_shape("content_push", {})
    assert exc.value.code == "missing_required_field"
    assert exc.value.field == "module_family_id"


# ── DB-bound reference validation ──────────────────────────────────────


@pytest.mark.asyncio
@requires_db
async def test_reference_gap_predicate_resolves_existing_active_gap(
    db_session: AsyncSession,
) -> None:
    code = f"ref_test_{uuid4().hex[:8]}"
    db_session.add(
        BehaviouralGap(gap_code=code, description=code, domain="hypertension", detection_rule_jsonb={})
    )
    await db_session.flush()
    await validate_predicate_references(
        db_session, trigger_kind="gap", predicate={"behavioural_gap_code": code}
    )


@pytest.mark.asyncio
@requires_db
async def test_reference_gap_predicate_unknown_gap_rejected(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(PredicateError) as exc:
        await validate_predicate_references(
            db_session,
            trigger_kind="gap",
            predicate={"behavioural_gap_code": f"nope_{uuid4().hex[:8]}"},
        )
    assert exc.value.code == "behavioural_gap_not_found"


@pytest.mark.asyncio
@requires_db
async def test_reference_gap_predicate_inactive_gap_rejected(
    db_session: AsyncSession,
) -> None:
    code = f"ref_inactive_{uuid4().hex[:8]}"
    db_session.add(
        BehaviouralGap(
            gap_code=code,
            description=code,
            domain="hypertension",
            detection_rule_jsonb={},
            status="deprecated",
        )
    )
    await db_session.flush()
    with pytest.raises(PredicateError) as exc:
        await validate_predicate_references(
            db_session, trigger_kind="gap", predicate={"behavioural_gap_code": code}
        )
    assert exc.value.code == "behavioural_gap_inactive"


@pytest.mark.asyncio
@requires_db
async def test_reference_content_push_resolves_existing_family(
    db_session: AsyncSession,
) -> None:
    family = ModuleFamily(module_code=f"PUSH-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()
    await validate_predicate(
        db_session,
        trigger_kind="content_push",
        predicate={"module_family_id": str(family.id)},
    )


@pytest.mark.asyncio
@requires_db
async def test_reference_content_push_unknown_family_rejected(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(PredicateError) as exc:
        await validate_predicate_references(
            db_session,
            trigger_kind="content_push",
            predicate={"module_family_id": str(uuid4())},
        )
    assert exc.value.code == "module_family_not_found"


@pytest.mark.asyncio
@requires_db
async def test_reference_content_push_invalid_uuid_rejected(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(PredicateError) as exc:
        await validate_predicate_references(
            db_session,
            trigger_kind="content_push",
            predicate={"module_family_id": "not-a-uuid"},
        )
    assert exc.value.code == "invalid_uuid"


@pytest.mark.asyncio
@requires_db
async def test_workflow_event_no_reference_check_needed(db_session: AsyncSession) -> None:
    """workflow_event predicates have no DB references to validate yet."""
    await validate_predicate(
        db_session,
        trigger_kind="workflow_event",
        predicate={"spice_event_code": "assessment_submitted"},
    )
