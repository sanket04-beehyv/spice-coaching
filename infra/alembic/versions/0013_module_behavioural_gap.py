"""Module ↔ behavioural_gap junction (multi-gap mapping).

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-03

Adds ``module_behavioural_gap`` so each module version can map to multiple
gaps. Backfills from existing ``module.primary_gap_id``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "module_behavioural_gap",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "module_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "behavioural_gap_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("behavioural_gap.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.UniqueConstraint(
            "module_id",
            "behavioural_gap_id",
            name="uq_module_behavioural_gap_pair",
        ),
    )
    op.create_index(
        "ix_module_behavioural_gap_module_id",
        "module_behavioural_gap",
        ["module_id"],
        unique=False,
    )
    op.create_index(
        "ix_module_behavioural_gap_behavioural_gap_id",
        "module_behavioural_gap",
        ["behavioural_gap_id"],
        unique=False,
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_module_behavioural_gap_one_primary
        ON module_behavioural_gap (module_id)
        WHERE is_primary
        """
    )
    op.execute(
        """
        INSERT INTO module_behavioural_gap (module_id, behavioural_gap_id, is_primary)
        SELECT id, primary_gap_id, true
        FROM module
        WHERE primary_gap_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_module_behavioural_gap_one_primary")
    op.drop_index("ix_module_behavioural_gap_behavioural_gap_id", table_name="module_behavioural_gap")
    op.drop_index("ix_module_behavioural_gap_module_id", table_name="module_behavioural_gap")
    op.drop_table("module_behavioural_gap")
