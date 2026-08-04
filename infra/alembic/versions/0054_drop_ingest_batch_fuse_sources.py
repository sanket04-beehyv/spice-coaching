"""Drop ingest_batch.fuse_sources — fusion is derived from source count.

Revision ID: 0054
Revises: 0053
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("ingest_batch", "fuse_sources")


def downgrade() -> None:
    op.add_column(
        "ingest_batch",
        sa.Column("fuse_sources", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.alter_column("ingest_batch", "fuse_sources", server_default=None)
