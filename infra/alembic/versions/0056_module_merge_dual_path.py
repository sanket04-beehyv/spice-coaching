"""Add module merge dual-path FK columns.

Revision ID: 0056
Revises: 0055
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "module",
        sa.Column("merge_secondary_module_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "module",
        sa.Column("merge_primary_module_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "module",
        sa.Column("merge_source_module_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("module", "merge_source_module_id")
    op.drop_column("module", "merge_primary_module_id")
    op.drop_column("module", "merge_secondary_module_id")
