"""Source document storage audit fields (hash, original filename, uploader id).

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-13

Adds nullable columns so ingestion and future admin flows can record
provenance without breaking existing rows. Populated by ingest endpoints
where bytes are known.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_document", sa.Column("content_sha256", sa.Text(), nullable=True))
    op.add_column("source_document", sa.Column("original_filename", sa.Text(), nullable=True))
    op.add_column("source_document", sa.Column("uploaded_by", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_document", "uploaded_by")
    op.drop_column("source_document", "original_filename")
    op.drop_column("source_document", "content_sha256")
