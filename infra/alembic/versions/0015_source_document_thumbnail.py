"""Add source_document.thumbnail_storage_path for ingest previews.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-04

Nullable MinIO path to a PNG thumbnail (first page / frame / waveform).
Populated by the platform.generate_source_thumbnail Celery task.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_document", sa.Column("thumbnail_storage_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_document", "thumbnail_storage_path")
