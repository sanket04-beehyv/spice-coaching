"""Add chw_gap_telemetry_event idempotency ledger for gap-state updates.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chw_gap_telemetry_event",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("chw_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.PrimaryKeyConstraint("event_id", name="pk_chw_gap_telemetry_event"),
    )


def downgrade() -> None:
    op.drop_table("chw_gap_telemetry_event")
