"""Partial unique index: one active non-fusion ingest run per source document.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX uq_ingestion_run_active_per_source
        ON ingestion_run (source_document_id)
        WHERE status = 'running'
          AND COALESCE(error_jsonb->>'type', '') != 'cross_source_fusion'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_ingestion_run_active_per_source")
