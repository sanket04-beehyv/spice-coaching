"""Add source_document.sync_published_visible for published sync gating.

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-25

When false (default), the document is excluded from GET /sync/source-documents/published.
Set per file at admin ingest time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_document",
        sa.Column(
            "sync_published_visible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.alter_column("source_document", "sync_published_visible", server_default=None)


def downgrade() -> None:
    op.drop_column("source_document", "sync_published_visible")
