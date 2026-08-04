"""Drop source_document.authority_label — no longer used at ingest or in APIs.

Revision ID: 0045
Revises: 0044
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("source_document", "authority_label")


def downgrade() -> None:
    op.add_column(
        "source_document",
        sa.Column("authority_label", sa.Text(), nullable=False, server_default=""),
    )
    op.alter_column("source_document", "authority_label", server_default=None)
