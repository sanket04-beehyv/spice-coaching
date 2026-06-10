"""CHW module quiz progress — per-question correct tracking.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chw_module_quiz_progress",
        sa.Column("chw_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "module_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "quiz_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_quiz_question.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "first_correct_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "chw_id",
            "module_id",
            "quiz_id",
            name="pk_chw_module_quiz_progress",
        ),
    )
    op.create_index(
        "ix_chw_module_quiz_progress_chw_module",
        "chw_module_quiz_progress",
        ["chw_id", "module_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chw_module_quiz_progress_chw_module", table_name="chw_module_quiz_progress")
    op.drop_table("chw_module_quiz_progress")
