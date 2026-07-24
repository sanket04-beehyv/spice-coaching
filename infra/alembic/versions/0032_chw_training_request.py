"""CHW training request table for module training requests.

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chw_training_request",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chw_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "module_family_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_family.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer_notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chw_training_request_chw_id",
        "chw_training_request",
        ["chw_id"],
    )
    op.create_index(
        "ix_chw_training_request_module_family_id",
        "chw_training_request",
        ["module_family_id"],
    )
    op.create_index(
        "ix_chw_training_request_tenant_status_submitted",
        "chw_training_request",
        ["tenant_id", "status", "submitted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chw_training_request_tenant_status_submitted", table_name="chw_training_request")
    op.drop_index("ix_chw_training_request_module_family_id", table_name="chw_training_request")
    op.drop_index("ix_chw_training_request_chw_id", table_name="chw_training_request")
    op.drop_table("chw_training_request")
