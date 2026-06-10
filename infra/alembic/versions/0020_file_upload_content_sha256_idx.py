"""Index on file_upload (bucket_name, content_sha256) for upload deduplication.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_file_upload_bucket_content_sha256",
        "file_upload",
        ["bucket_name", "content_sha256"],
    )


def downgrade() -> None:
    op.drop_index("ix_file_upload_bucket_content_sha256", table_name="file_upload")
