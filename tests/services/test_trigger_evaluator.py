"""W-8 — trigger_evaluator: pure-unit tests of gap-trigger firing logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from platform_service.db.models.chw_behavioural_gap_state import CHWBehaviouralGapState
from platform_service.services.trigger_evaluator import (
    evaluate_gap_trigger,
    should_reset_window,
)


def _state(
    *,
    occurrence_count: int = 0,
    last_observed_at: datetime | None = None,
    severity_current: str = "moderate",
    status: str = "active",
) -> CHWBehaviouralGapState:
    return CHWBehaviouralGapState(
        chw_id=uuid4().int % (10**15) + 1,
        behavioural_gap_id=uuid4(),
        occurrence_count=occurrence_count,
        last_observed_at=last_observed_at,
        severity_current=severity_current,
        status=status,
    )


# ── should_reset_window ───────────────────────────────────────────────


def test_window_reset_no_prior_observation() -> None:
    state = _state()
    assert should_reset_window(state, {"window_days": 14}) is False


def test_window_reset_inside_window() -> None:
    state = _state(last_observed_at=datetime.now(UTC) - timedelta(days=7))
    assert should_reset_window(state, {"window_days": 14}) is False


def test_window_reset_outside_window() -> None:
    state = _state(last_observed_at=datetime.now(UTC) - timedelta(days=15))
    assert should_reset_window(state, {"window_days": 14}) is True


# ── evaluate_gap_trigger ──────────────────────────────────────────────


def test_fires_when_threshold_reached_within_window() -> None:
    state = _state(
        occurrence_count=2,
        last_observed_at=datetime.now(UTC) - timedelta(days=3),
    )
    decision = evaluate_gap_trigger(state, {"occurrences_threshold": 2, "window_days": 14})
    assert decision.fired is True
    assert decision.reason == "fired"


def test_does_not_fire_below_threshold() -> None:
    state = _state(
        occurrence_count=1,
        last_observed_at=datetime.now(UTC) - timedelta(days=3),
    )
    decision = evaluate_gap_trigger(state, {"occurrences_threshold": 2, "window_days": 14})
    assert decision.fired is False
    assert decision.reason == "below_threshold"


def test_does_not_fire_outside_window() -> None:
    """Edge case #2: 2nd occurrence on day 15 → counter reset, no trigger.

    The evaluator just reports `outside_window`; the GapStateService is
    responsible for resetting the counter on the next observation.
    """
    state = _state(
        occurrence_count=2,
        last_observed_at=datetime.now(UTC) - timedelta(days=20),
    )
    decision = evaluate_gap_trigger(state, {"occurrences_threshold": 2, "window_days": 14})
    assert decision.fired is False
    assert decision.reason == "outside_window"


def test_suppressed_state_never_fires() -> None:
    state = _state(
        occurrence_count=10,
        last_observed_at=datetime.now(UTC),
        status="suppressed",
    )
    decision = evaluate_gap_trigger(state, {"occurrences_threshold": 2, "window_days": 14})
    assert decision.fired is False
    assert decision.reason == "suppressed"


def test_severity_floor_blocks_low_severity() -> None:
    state = _state(
        occurrence_count=5,
        last_observed_at=datetime.now(UTC),
        severity_current="low",
    )
    decision = evaluate_gap_trigger(
        state,
        {"occurrences_threshold": 2, "window_days": 14, "severity_floor": "high"},
    )
    assert decision.fired is False
    assert decision.reason == "severity_floor"


def test_severity_floor_allows_at_or_above_floor() -> None:
    state = _state(
        occurrence_count=2,
        last_observed_at=datetime.now(UTC),
        severity_current="moderate",
    )
    decision = evaluate_gap_trigger(
        state,
        {"occurrences_threshold": 2, "window_days": 14, "severity_floor": "moderate"},
    )
    assert decision.fired is True


def test_predicate_falls_back_to_settings_defaults() -> None:
    """Empty predicate → use the configured defaults from settings."""
    state = _state(occurrence_count=2, last_observed_at=datetime.now(UTC) - timedelta(days=3))
    decision = evaluate_gap_trigger(state, {})
    # Default occurrences_threshold from settings is 2 (gap_trigger_default_occurrences).
    assert decision.fired is True
    assert decision.occurrences_threshold == 2
    assert decision.window_days == 14
