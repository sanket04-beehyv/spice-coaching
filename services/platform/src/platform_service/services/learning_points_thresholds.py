"""Learning point amounts — keys and defaults for `config_threshold` rows.

Runtime values are read from Postgres (`config_threshold`); this module holds
stable key strings, fallback defaults when a row is absent, and pure delta
logic for telemetry `event_type`.
"""

from __future__ import annotations

from collections.abc import Mapping

# Keys stored in `config_threshold.key` (also exposed via config sync).
KEY_LEARNING_POINTS_MODULE_DELIVERED = "learning_points_module_delivered"
KEY_LEARNING_POINTS_MODULE_CARD_VIEWED = "learning_points_module_card_viewed"
KEY_LEARNING_POINTS_MODULE_QUIZ_ATTEMPTED_BASE = "learning_points_module_quiz_attempted_base"
KEY_LEARNING_POINTS_MODULE_QUIZ_SCORE_MULTIPLIER = "learning_points_module_quiz_score_multiplier"
KEY_LEARNING_POINTS_MODULE_COMPLETED = "learning_points_module_completed"
KEY_LEARNING_POINTS_SPICE_ACTION_OBSERVED = "learning_points_spice_action_observed"

LEARNING_POINTS_THRESHOLD_DEFAULTS: dict[str, int] = {
    KEY_LEARNING_POINTS_MODULE_DELIVERED: 5,
    KEY_LEARNING_POINTS_MODULE_CARD_VIEWED: 2,
    KEY_LEARNING_POINTS_MODULE_QUIZ_ATTEMPTED_BASE: 5,
    KEY_LEARNING_POINTS_MODULE_QUIZ_SCORE_MULTIPLIER: 15,
    KEY_LEARNING_POINTS_MODULE_COMPLETED: 20,
    KEY_LEARNING_POINTS_SPICE_ACTION_OBSERVED: 3,
}


def learning_points_delta_for_event(
    event_type: str,
    *,
    quiz_score_pct: float | None,
    thresholds: Mapping[str, int],
) -> int:
    """Return points to award for this `event_type`, or 0 if not scored.

    `thresholds` must supply the six learning-points keys (typically merged
    from `config_threshold` with `LEARNING_POINTS_THRESHOLD_DEFAULTS`).
    """
    key = (event_type or "").strip().lower()
    if key == "module_delivered":
        return max(0, thresholds[KEY_LEARNING_POINTS_MODULE_DELIVERED])
    if key == "module_card_viewed":
        return max(0, thresholds[KEY_LEARNING_POINTS_MODULE_CARD_VIEWED])
    if key == "module_completed":
        return max(0, thresholds[KEY_LEARNING_POINTS_MODULE_COMPLETED])
    if key == "spice_action_observed":
        return max(0, thresholds[KEY_LEARNING_POINTS_SPICE_ACTION_OBSERVED])
    if key == "module_quiz_attempted":
        base = max(0, thresholds[KEY_LEARNING_POINTS_MODULE_QUIZ_ATTEMPTED_BASE])
        bonus = 0
        if quiz_score_pct is not None:
            pct = max(0.0, min(1.0, float(quiz_score_pct)))
            bonus = int(pct * thresholds[KEY_LEARNING_POINTS_MODULE_QUIZ_SCORE_MULTIPLIER])
        return base + bonus
    return 0
