"""W-8 — Server-side gap-trigger evaluator.

Pure-logic helpers (no DB). Given a CHW's `chw_behavioural_gap_state` row
and a gap-kind predicate, decide:

- Should the occurrence counter be reset (window expired)?
- Has the trigger fired (occurrence_count ≥ threshold within the window)?

Workflow-event triggers are evaluated by the SDK (per Architecture R8 the
SPICE app pushes workflow events), so the server-side evaluator covers only
`gap` and (downstream) `content_push`. Content_push fires once per published
module version; that flow lives in the publisher (W-6) and the sync API (W-9).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from platform_service.config import get_settings
from platform_service.db.models.chw_behavioural_gap_state import CHWBehaviouralGapState

_SEVERITY_RANK = {"low": 0, "moderate": 1, "high": 2}


@dataclass(frozen=True)
class GapTriggerDecision:
    """Outcome of evaluating a gap predicate against a CHW gap state."""

    fired: bool
    reason: str  # "fired" | "below_threshold" | "outside_window" | "severity_floor" | "suppressed"
    occurrences_threshold: int
    window_days: int


def _now() -> datetime:
    return datetime.now(UTC)


def _resolve_thresholds(predicate: dict[str, Any]) -> tuple[int, int]:
    """Pull (occurrences_threshold, window_days) from predicate, falling back
    to settings defaults."""
    settings = get_settings()
    occurrences = int(predicate.get("occurrences_threshold", settings.gap_trigger_default_occurrences))
    window_days = int(predicate.get("window_days", settings.gap_trigger_default_window_days))
    return occurrences, window_days


def should_reset_window(
    gap_state: CHWBehaviouralGapState,
    predicate: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """True if the most-recent observation is OUTSIDE the trigger window —
    in which case the caller should reset occurrence_count to 1 (this
    observation) before re-evaluating.

    Edge case (#2 from Implementation Plan §10): occurrence #2 lands on day 15
    when window is 14 days → window expired → counter reset, no trigger.
    """
    if gap_state.last_observed_at is None:
        return False
    _, window_days = _resolve_thresholds(predicate)
    cutoff = (now or _now()) - timedelta(days=window_days)
    return gap_state.last_observed_at < cutoff


def evaluate_gap_trigger(
    gap_state: CHWBehaviouralGapState,
    predicate: dict[str, Any],
    *,
    now: datetime | None = None,
) -> GapTriggerDecision:
    """Decide whether a gap trigger fires for this CHW + gap.

    Logic:
    1. If state.status='suppressed' → never fire (operator suppressed).
    2. If severity_floor set and current severity below it → don't fire.
    3. If last_observed outside window → window has expired (the caller is
       expected to have handled the reset before calling this evaluator;
       returns `outside_window`).
    4. If occurrence_count ≥ threshold → FIRE.
    5. Otherwise → below_threshold.
    """
    occurrences_threshold, window_days = _resolve_thresholds(predicate)
    decision_kwargs: dict[str, Any] = {
        "occurrences_threshold": occurrences_threshold,
        "window_days": window_days,
    }

    if gap_state.status == "suppressed":
        return GapTriggerDecision(fired=False, reason="suppressed", **decision_kwargs)

    severity_floor = predicate.get("severity_floor")
    if severity_floor is not None:
        current_rank = _SEVERITY_RANK.get(gap_state.severity_current, -1)
        floor_rank = _SEVERITY_RANK.get(severity_floor, -1)
        if current_rank < floor_rank:
            return GapTriggerDecision(fired=False, reason="severity_floor", **decision_kwargs)

    if should_reset_window(gap_state, predicate, now=now):
        return GapTriggerDecision(fired=False, reason="outside_window", **decision_kwargs)

    if gap_state.occurrence_count >= occurrences_threshold:
        return GapTriggerDecision(fired=True, reason="fired", **decision_kwargs)

    return GapTriggerDecision(fired=False, reason="below_threshold", **decision_kwargs)
