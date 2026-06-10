"""Parse and normalise telemetry payload fields for module completion handlers."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def parse_quiz_score_pct(value: object) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def parse_chw_id(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            logger.warning("module_completion: invalid chw_id=%r", value)
            return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        logger.warning("module_completion: invalid chw_id=%r", value)
        return None


def parse_uuid(value: object, *, field: str) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        logger.warning("module_completion: invalid UUID for %s=%r", field, value)
        return None


def coerce_tenant_uuid(value: object) -> UUID | None:
    """tenant_id arrives as int (legacy) or str (uuid). Only forward the UUID
    flavour to chw_module_completion.tenant_id (which is UUID-typed in the
    v3.3 schema). Integer tenant ids stay legacy-only."""
    if value is None or isinstance(value, int):
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def module_quiz_outcome_kind(payload: dict[str, Any]) -> str | None:
    """How to adjust gap ``failed_attempts_count`` for MODULE_QUIZ_ATTEMPTED.

    Returns ``'correct'``, ``'incorrect'``, or ``None`` (fall back to
    score-based pass/fail for the gap). Mirrors SPICE: top-level ``outcome``
    or nested ``payload_json.outcome``.
    """
    raw = payload.get("outcome")
    if raw is None:
        nested = payload.get("payload_json")
        if isinstance(nested, dict):
            raw = nested.get("outcome")
    if raw is None:
        return None
    value = getattr(raw, "value", raw)
    normalized = str(value).strip().lower()
    if normalized == "correct":
        return "correct"
    if normalized in ("wrong", "incorrect"):
        return "incorrect"
    return None


def parse_quiz_id(payload: dict[str, Any]) -> UUID | None:
    """For module_quiz_attempted, extract ``module_quiz_question.id`` from the event."""
    return parse_uuid(payload.get("quiz_id"), field="quiz_id")


def spice_outcome_is_incorrect(payload: dict[str, Any], payload_json: dict[str, Any]) -> bool:
    """True when coaching outcome is a hard miss (`Outcome.WRONG` / `INCORRECT`).

    Accepts outcome on the Celery job (mirrors telemetry top-level `outcome`)
    or under `payload_json` for clients that nest it there.
    """
    raw = payload.get("outcome")
    if raw is None:
        raw = payload_json.get("outcome")
    if raw is None:
        return False
    value = getattr(raw, "value", raw)
    normalized = str(value).strip().lower()
    return normalized in ("wrong", "incorrect")
