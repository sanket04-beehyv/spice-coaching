"""CHW quiz question state — per-question coaching state (quiz-id telemetry mode).

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chw_quiz_question_state",
        sa.Column("chw_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "quiz_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_quiz_question.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "module_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("failed_attempts_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_failed_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "escalated_to_supervisor",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("chw_id", "quiz_id", name="pk_chw_quiz_question_state"),
    )
    op.create_index(
        "ix_chw_quiz_question_state_chw_module",
        "chw_quiz_question_state",
        ["chw_id", "module_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_chw_quiz_question_state_chw_module", table_name="chw_quiz_question_state")
    op.drop_table("chw_quiz_question_state")
