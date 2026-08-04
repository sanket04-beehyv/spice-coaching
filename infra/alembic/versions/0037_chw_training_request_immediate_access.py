"""CHW training requests grant immediate module access (no admin review).

Downgrade note:
- Rows created with only `requested_module_name` cannot be represented in the
  legacy schema (which requires `module_family_id`). Downgrade removes them.

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chw_training_request",
        sa.Column("requested_module_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "chw_training_request",
        sa.Column("module_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_chw_training_request_module_id_module",
        "chw_training_request",
        "module",
        ["module_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_chw_training_request_module_id",
        "chw_training_request",
        ["module_id"],
    )
    op.execute(
        """
        UPDATE chw_training_request
        SET module_id = mf.current_published_module_id
        FROM module_family mf
        WHERE mf.id = chw_training_request.module_family_id
          AND mf.current_published_module_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE chw_training_request
        SET requested_module_name = COALESCE(
            requested_module_name,
            'Legacy module family request (no published module at migration time)'
        )
        WHERE module_id IS NULL
          AND requested_module_name IS NULL
        """
    )
    op.drop_index("ix_chw_training_request_module_family_id", table_name="chw_training_request")
    op.drop_index("ix_chw_training_request_tenant_status_submitted", table_name="chw_training_request")
    op.drop_column("chw_training_request", "module_family_id")
    op.drop_column("chw_training_request", "status")
    op.create_index(
        "ix_chw_training_request_tenant_submitted",
        "chw_training_request",
        ["tenant_id", "submitted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chw_training_request_tenant_submitted", table_name="chw_training_request")
    op.add_column(
        "chw_training_request",
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
    )
    op.add_column(
        "chw_training_request",
        sa.Column("module_family_id", sa.UUID(), nullable=True),
    )
    op.execute(
        """
        UPDATE chw_training_request
        SET module_family_id = m.module_family_id
        FROM module m
        WHERE m.id = chw_training_request.module_id
        """
    )
    op.execute(
        """
        UPDATE chw_training_request
        SET status = 'active'
        WHERE status = 'pending'
        """
    )
    op.execute("DELETE FROM chw_training_request WHERE module_family_id IS NULL")
    op.alter_column("chw_training_request", "module_family_id", nullable=False)
    op.create_foreign_key(
        "chw_training_request_module_family_id_fkey",
        "chw_training_request",
        "module_family",
        ["module_family_id"],
        ["id"],
        ondelete="RESTRICT",
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
    op.drop_index("ix_chw_training_request_module_id", table_name="chw_training_request")
    op.drop_constraint("fk_chw_training_request_module_id_module", "chw_training_request", type_="foreignkey")
    op.drop_column("chw_training_request", "module_id")
    op.drop_column("chw_training_request", "requested_module_name")
