"""Add source_document.ingestion_instructions for Stage C steering.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-08

Optional admin-provided steering text, sanitized at ingest and injected
into Stage C module-identification prompts.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_document", sa.Column("ingestion_instructions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_document", "ingestion_instructions")
