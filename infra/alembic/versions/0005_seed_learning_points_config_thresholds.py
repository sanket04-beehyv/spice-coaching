"""Seed learning-points rows in config_threshold.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEARNING_POINT_KEYS = (
    "learning_points_module_delivered",
    "learning_points_module_card_viewed",
    "learning_points_module_quiz_attempted_base",
    "learning_points_module_quiz_score_multiplier",
    "learning_points_module_completed",
    "learning_points_spice_action_observed",
)


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO config_threshold (version, key, value_json, description) VALUES
        (1, 'learning_points_module_delivered', '5'::jsonb,
         'CHW learning points awarded per module_delivered telemetry event'),
        (1, 'learning_points_module_card_viewed', '10'::jsonb,
         'CHW learning points awarded per module_card_viewed telemetry event'),
        (1, 'learning_points_module_quiz_attempted_base', '15'::jsonb,
         'Base CHW learning points for module_quiz_attempted (correct outcome)'),
        (1, 'learning_points_module_quiz_score_multiplier', '15'::jsonb,
         'Quiz score bonus multiplier: floor(quiz_score_pct [0–1] * this) added to base'),
        (1, 'learning_points_module_completed', '20'::jsonb,
         'CHW learning points awarded per module_completed telemetry event'),
        (1, 'learning_points_spice_action_observed', '3'::jsonb,
         'CHW learning points awarded per spice_action_observed telemetry event')
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    keys = ", ".join(f"'{k}'" for k in _LEARNING_POINT_KEYS)
    op.execute(f"DELETE FROM config_threshold WHERE key IN ({keys})")
