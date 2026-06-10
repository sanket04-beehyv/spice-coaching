"""W-8 — Validate trigger predicates against per-kind schemas.

Two layers:

1. **Shape validation** (`validate_predicate_shape`): pure-Python check that
   the predicate has the right keys with the right types. No DB access.
   Raises PredicateError with a per-field reason.

2. **Reference validation** (`validate_predicate_references`): for `gap` and
   `content_push` predicates, confirms the referenced row exists and is
   active. Async, hits the DB.

Edge cases (Implementation Plan §10):
- Missing required field → invalid (shape)
- Wrong type → invalid (shape)
- Predicate references non-existent behavioural_gap → invalid (references)
- behavioural_gap exists but is deprecated → invalid (references)
- Predicate references non-existent module_family → invalid (references)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module_family import ModuleFamily
from platform_service.services.prompts.trigger_predicate_schemas import (
    VALID_TRIGGER_KINDS,
    schema_for,
)


class PredicateError(Exception):
    """Raised when a trigger predicate fails shape or reference validation."""

    def __init__(self, code: str, field: str | None, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


# ── Shape ───────────────────────────────────────────────────────────────


def validate_predicate_shape(trigger_kind: str, predicate: dict[str, Any]) -> None:
    """Check predicate against the per-kind schema. Raises PredicateError on
    the first violation."""
    if trigger_kind not in VALID_TRIGGER_KINDS:
        raise PredicateError("unknown_trigger_kind", None, f"trigger_kind {trigger_kind!r} not recognised")
    if not isinstance(predicate, dict):
        raise PredicateError(
            "predicate_not_object",
            None,
            f"predicate must be a JSON object, got {type(predicate).__name__}",
        )
    schema = schema_for(trigger_kind)

    required: tuple[str, ...] = schema["required"]
    optional: tuple[str, ...] = schema.get("optional", ())
    allowed = set(required) | set(optional)
    types: dict[str, type] = schema.get("types", {})
    value_constraints = schema.get("value_constraints", {})
    enums: dict[str, set[str]] = schema.get("enums", {})

    # 1. Unknown keys are rejected (catch typos before they cause silent misfires).
    for key in predicate:
        if key not in allowed:
            raise PredicateError(
                "unknown_field", key, f"unknown predicate field {key!r} for kind {trigger_kind!r}"
            )

    # 2. Required keys present.
    for key in required:
        if key not in predicate:
            raise PredicateError(
                "missing_required_field",
                key,
                f"predicate missing required field {key!r} for kind {trigger_kind!r}",
            )

    # 3. Type checks for present keys (required + optional).
    for key, value in predicate.items():
        expected = types.get(key)
        if expected is None:
            continue
        # Reject bools-as-ints up front (Python's bool is a subclass of int,
        # so the plain isinstance check below would let `True` through).
        if expected is int and isinstance(value, bool):
            raise PredicateError(
                "wrong_type",
                key,
                f"predicate field {key!r} must be int, got bool",
            )
        if not isinstance(value, expected):
            raise PredicateError(
                "wrong_type",
                key,
                f"predicate field {key!r} must be {expected.__name__}, got {type(value).__name__}",
            )

    # 4. Value constraints.
    for key, value in predicate.items():
        check = value_constraints.get(key)
        if check is None:
            continue
        err = check(value)
        if err is not None:
            raise PredicateError("value_constraint_failed", key, f"predicate field {key!r}: {err}")

    # 5. Enum constraints.
    for key, allowed_values in enums.items():
        if key in predicate and predicate[key] not in allowed_values:
            raise PredicateError(
                "enum_violation",
                key,
                f"predicate field {key!r} must be one of {sorted(allowed_values)}, got {predicate[key]!r}",
            )


# ── Reference validation (DB-bound) ────────────────────────────────────


async def validate_predicate_references(
    session: AsyncSession,
    *,
    trigger_kind: str,
    predicate: dict[str, Any],
) -> None:
    """Confirm referenced rows exist and are active. Call AFTER shape passes."""
    if trigger_kind == "gap":
        code = predicate["behavioural_gap_code"]
        result = await session.execute(select(BehaviouralGap).where(BehaviouralGap.gap_code == code))
        gap = result.scalar_one_or_none()
        if gap is None:
            raise PredicateError(
                "behavioural_gap_not_found",
                "behavioural_gap_code",
                f"behavioural_gap {code!r} does not exist",
            )
        if gap.status != "active":
            raise PredicateError(
                "behavioural_gap_inactive",
                "behavioural_gap_code",
                f"behavioural_gap {code!r} has status={gap.status!r} (must be 'active')",
            )
    elif trigger_kind == "content_push":
        family_id_str = predicate["module_family_id"]
        try:
            family_id = UUID(family_id_str)
        except ValueError as exc:
            raise PredicateError(
                "invalid_uuid",
                "module_family_id",
                f"module_family_id is not a valid UUID: {exc}",
            ) from exc
        family = await session.get(ModuleFamily, family_id)
        if family is None:
            raise PredicateError(
                "module_family_not_found",
                "module_family_id",
                f"module_family {family_id} does not exist",
            )
    # workflow_event: nothing to validate against; spice_event_code is a free-form
    # string until W-12 SPICE research produces a registry.


async def validate_predicate(
    session: AsyncSession,
    *,
    trigger_kind: str,
    predicate: dict[str, Any],
) -> None:
    """Run shape then reference validation."""
    validate_predicate_shape(trigger_kind, predicate)
    await validate_predicate_references(session, trigger_kind=trigger_kind, predicate=predicate)
