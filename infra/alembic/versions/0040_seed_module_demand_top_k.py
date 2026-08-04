"""Seed module_demand_top_k config threshold.

Revision ID: 0040
Revises: 0039
Create Date: 2026-07-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO config_threshold (version, key, value_json, title, description) VALUES
        (1, 'module_demand_top_k', '10'::jsonb,
         'Module Demand Top K',
         'Number of top requested modules shown in the admin module demand summary.')
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM config_threshold WHERE key = 'module_demand_top_k'")
