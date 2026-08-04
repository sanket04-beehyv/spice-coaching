"""Seed quiz reattempt validity days and add title in config_threshold.

Revision ID: 0036
Revises: 0035
Create Date: 2026-07-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE config_threshold ADD COLUMN IF NOT EXISTS title TEXT")

    op.execute(
        """
        UPDATE config_threshold SET title = 'Learning Points: Module Delivered'
        WHERE key = 'learning_points_module_delivered' AND title IS NULL
        """
    )
    op.execute(
        """
        UPDATE config_threshold SET title = 'Learning Points: Module Card Viewed'
        WHERE key = 'learning_points_module_card_viewed' AND title IS NULL
        """
    )
    op.execute(
        """
        UPDATE config_threshold SET title = 'Learning Points: Quiz Attempted (Base)'
        WHERE key = 'learning_points_module_quiz_attempted_base' AND title IS NULL
        """
    )
    op.execute(
        """
        UPDATE config_threshold SET title = 'Learning Points: Quiz Score Multiplier'
        WHERE key = 'learning_points_module_quiz_score_multiplier' AND title IS NULL
        """
    )
    op.execute(
        """
        UPDATE config_threshold SET title = 'Learning Points: Module Completed'
        WHERE key = 'learning_points_module_completed' AND title IS NULL
        """
    )
    op.execute(
        """
        UPDATE config_threshold SET title = 'Learning Points: Spice Action Observed'
        WHERE key = 'learning_points_spice_action_observed' AND title IS NULL
        """
    )

    op.execute(
        """
        INSERT INTO config_threshold (version, key, value_json, title, description) VALUES
        (1, 'quiz_reattempt_validity_days', '7'::jsonb,
         'Quiz Reattempt Validity (Days)',
         'Configure the number of days from the module assignment date during which users can reattempt a quiz. Users are always allowed their first quiz attempt, even if this period has expired. After the first attempt, reattempts are permitted only until the configured validity period ends.')
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM config_threshold WHERE key = 'quiz_reattempt_validity_days'")
    op.execute("ALTER TABLE config_threshold DROP COLUMN IF EXISTS title")
