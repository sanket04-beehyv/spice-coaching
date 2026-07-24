"""module admin lifecycle via lifecycle_status + lifecycle event audit log.

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "module",
        sa.Column("first_activated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "module",
        sa.Column("last_deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "module",
        sa.Column("last_reactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "module",
        sa.Column("deactivated_by", sa.UUID(), nullable=True),
    )
    op.add_column(
        "module",
        sa.Column("reactivated_by", sa.UUID(), nullable=True),
    )

    op.execute(
        """
        UPDATE module
        SET first_activated_at = published_at
        WHERE lifecycle_status = 'published' AND published_at IS NOT NULL
        """
    )

    op.create_table(
        "module_lifecycle_event",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "module_id",
            sa.UUID(),
            sa.ForeignKey("module.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_module_lifecycle_event_module_occurred",
        "module_lifecycle_event",
        ["module_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_module_lifecycle_event_module_occurred", table_name="module_lifecycle_event")
    op.drop_table("module_lifecycle_event")
    op.drop_column("module", "reactivated_by")
    op.drop_column("module", "deactivated_by")
    op.drop_column("module", "last_reactivated_at")
    op.drop_column("module", "last_deactivated_at")
    op.drop_column("module", "first_activated_at")
