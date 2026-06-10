"""chw_id: uuid -> bigint on CHW state tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-12

PostgreSQL cannot cast uuid to bigint. This migration drops and re-adds
`chw_id` on empty `chw_behavioural_gap_state` / `chw_module_completion`
tables (greenfield). If rows exist, truncate those tables before upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("pk_chw_behavioural_gap_state", "chw_behavioural_gap_state", type_="primary")
    op.drop_column("chw_behavioural_gap_state", "chw_id")
    op.add_column("chw_behavioural_gap_state", sa.Column("chw_id", sa.BigInteger(), nullable=False))
    op.create_primary_key(
        "pk_chw_behavioural_gap_state",
        "chw_behavioural_gap_state",
        ["chw_id", "behavioural_gap_id"],
    )

    op.drop_constraint("pk_chw_module_completion", "chw_module_completion", type_="primary")
    op.drop_column("chw_module_completion", "chw_id")
    op.add_column("chw_module_completion", sa.Column("chw_id", sa.BigInteger(), nullable=False))
    op.create_primary_key(
        "pk_chw_module_completion",
        "chw_module_completion",
        ["chw_id", "module_family_id"],
    )


def downgrade() -> None:
    op.drop_constraint("pk_chw_module_completion", "chw_module_completion", type_="primary")
    op.drop_column("chw_module_completion", "chw_id")
    op.add_column(
        "chw_module_completion",
        sa.Column("chw_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.create_primary_key(
        "pk_chw_module_completion",
        "chw_module_completion",
        ["chw_id", "module_family_id"],
    )

    op.drop_constraint("pk_chw_behavioural_gap_state", "chw_behavioural_gap_state", type_="primary")
    op.drop_column("chw_behavioural_gap_state", "chw_id")
    op.add_column(
        "chw_behavioural_gap_state",
        sa.Column("chw_id", postgresql.UUID(as_uuid=True), nullable=False),
    )
    op.create_primary_key(
        "pk_chw_behavioural_gap_state",
        "chw_behavioural_gap_state",
        ["chw_id", "behavioural_gap_id"],
    )
