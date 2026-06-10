"""W-8 — Trigger predicate shape definitions.

One per `trigger_kind`. Hand-rolled rather than using `jsonschema`: the
shapes are tiny and stable, and we want validation errors that point at the
specific missing field rather than a generic schema-violation message.

Each schema describes:
- `required`: keys that MUST be present
- `optional`: keys that MAY be present
- `types`: per-key expected Python type (or tuple of types)
- `value_constraints`: per-key callable(value) → str | None (returns error msg)

Per Pipeline §16 / Architecture R8 / Data Model §6.2.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# ── Constraint helpers ──────────────────────────────────────────────────


def _positive_int(value: Any) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return f"must be a positive integer, got {value!r}"
    return None


def _non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return f"must be a non-empty string, got {value!r}"
    return None


# ── Schemas ────────────────────────────────────────────────────────────


# Gap trigger fires when a behavioural-gap pattern crosses the threshold
# within the configured window. Per Architecture §9.1 R8 the defaults are
# `occurrences=2, window_days=14` (settings-overridable).
GAP_PREDICATE_SCHEMA: dict[str, Any] = {
    "kind": "gap",
    "required": ("behavioural_gap_code",),
    "optional": ("occurrences_threshold", "window_days", "severity_floor"),
    "types": {
        "behavioural_gap_code": str,
        "occurrences_threshold": int,
        "window_days": int,
        "severity_floor": str,
    },
    "value_constraints": {
        "behavioural_gap_code": _non_empty_string,
        "occurrences_threshold": _positive_int,
        "window_days": _positive_int,
    },
    "enums": {
        "severity_floor": {"low", "moderate", "high"},
    },
}

# Workflow event trigger fires when a SPICE workflow event matching the
# spice_event_code is observed for the CHW. The optional `filter_predicate`
# narrows the match (e.g. "outcome=referred" or "patient_age_group=child").
# `spice_event_code` values come from the SPICE SDK research (W-12).
WORKFLOW_EVENT_PREDICATE_SCHEMA: dict[str, Any] = {
    "kind": "workflow_event",
    "required": ("spice_event_code",),
    "optional": ("filter_predicate",),
    "types": {
        "spice_event_code": str,
        "filter_predicate": dict,
    },
    "value_constraints": {
        "spice_event_code": _non_empty_string,
    },
}

# Content push trigger fires when a new published version of the named
# module_family becomes available. Bound modules are pushed to all CHWs
# via /sync regardless of completion state.
CONTENT_PUSH_PREDICATE_SCHEMA: dict[str, Any] = {
    "kind": "content_push",
    "required": ("module_family_id",),
    "optional": ("minimum_version",),
    "types": {
        "module_family_id": str,  # UUID string
        "minimum_version": int,
    },
    "value_constraints": {
        "module_family_id": _non_empty_string,
        "minimum_version": _positive_int,
    },
}


SCHEMA_BY_KIND: dict[str, dict[str, Any]] = {
    "gap": GAP_PREDICATE_SCHEMA,
    "workflow_event": WORKFLOW_EVENT_PREDICATE_SCHEMA,
    "content_push": CONTENT_PUSH_PREDICATE_SCHEMA,
}

VALID_TRIGGER_KINDS: frozenset[str] = frozenset(SCHEMA_BY_KIND.keys())


def schema_for(trigger_kind: str) -> dict[str, Any]:
    """Return the schema for a trigger_kind. Raises KeyError if unknown."""
    return SCHEMA_BY_KIND[trigger_kind]


__all__ = (
    "GAP_PREDICATE_SCHEMA",
    "WORKFLOW_EVENT_PREDICATE_SCHEMA",
    "CONTENT_PUSH_PREDICATE_SCHEMA",
    "SCHEMA_BY_KIND",
    "VALID_TRIGGER_KINDS",
    "schema_for",
)


# Re-export the constraint helpers for tests.
_ConstraintFn = Callable[[Any], str | None]
__all__ += ("_ConstraintFn",)
