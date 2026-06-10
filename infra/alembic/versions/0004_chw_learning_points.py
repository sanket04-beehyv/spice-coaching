"""CHW learning points — single table (per-event row, SUM for totals).

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chw_learning_point_event",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chw_id", sa.BigInteger(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("awarded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name="pk_chw_learning_point_event"),
    )
    op.create_index(
        "ix_chw_learning_point_event_chw_id",
        "chw_learning_point_event",
        ["chw_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chw_learning_point_event_chw_id", table_name="chw_learning_point_event")
    op.drop_table("chw_learning_point_event")
