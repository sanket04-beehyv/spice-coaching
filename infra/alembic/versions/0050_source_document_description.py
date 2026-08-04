"""Add source_document.description for post-upload metadata.

Revision ID: 0050
Revises: 0049
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_document", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_document", "description")
