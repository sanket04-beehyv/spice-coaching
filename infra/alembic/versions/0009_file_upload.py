"""file_upload — canonical MinIO upload provenance.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-23

Captures upload-time metadata (hash, original filename, uploader subject)
so ``/ingest/from-object`` does not have to recover provenance from a
UUID-prefixed object key basename.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_upload",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("bucket_name", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("bucket_name", "object_key", name="uq_file_upload_object"),
    )
    op.create_index("ix_file_upload_storage_path", "file_upload", ["storage_path"])


def downgrade() -> None:
    op.drop_index("ix_file_upload_storage_path", table_name="file_upload")
    op.drop_table("file_upload")
