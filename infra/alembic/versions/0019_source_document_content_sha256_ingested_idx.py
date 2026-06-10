"""Partial index on source_document.content_sha256 for ingested rows.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_source_document_content_sha256_ingested
        ON source_document (content_sha256)
        WHERE status = 'ingested' AND content_sha256 IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_source_document_content_sha256_ingested")
