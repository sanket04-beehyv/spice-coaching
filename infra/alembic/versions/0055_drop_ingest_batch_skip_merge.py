"""Drop ingest_batch.skip_merge — merge is always on for normal ingest.

Revision ID: 0055
Revises: 0054
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("ingest_batch", "skip_merge")


def downgrade() -> None:
    op.add_column(
        "ingest_batch",
        sa.Column("skip_merge", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.alter_column("ingest_batch", "skip_merge", server_default=None)
