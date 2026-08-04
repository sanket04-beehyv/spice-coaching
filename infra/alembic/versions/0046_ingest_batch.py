"""Add ingest_batch and link ingestion_run; allow queued runs.

Revision ID: 0046
Revises: 0045
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingest_batch",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("fuse_sources", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("skip_merge", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_jsonb", postgresql.JSONB(), nullable=True),
        sa.Column("triggered_by", postgresql.UUID(as_uuid=True), nullable=True),
    )

    op.add_column(
        "ingestion_run",
        sa.Column(
            "ingest_batch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingest_batch.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_ingestion_run_ingest_batch_id",
        "ingestion_run",
        ["ingest_batch_id"],
    )

    op.execute("DROP INDEX IF EXISTS uq_ingestion_run_active_per_source")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_ingestion_run_active_per_source
        ON ingestion_run (source_document_id)
        WHERE status IN ('queued', 'running')
          AND COALESCE(error_jsonb->>'type', '') != 'cross_source_fusion'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_ingestion_run_active_per_source")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_ingestion_run_active_per_source
        ON ingestion_run (source_document_id)
        WHERE status = 'running'
          AND COALESCE(error_jsonb->>'type', '') != 'cross_source_fusion'
        """
    )
    op.drop_index("ix_ingestion_run_ingest_batch_id", table_name="ingestion_run")
    op.drop_column("ingestion_run", "ingest_batch_id")
    op.drop_table("ingest_batch")
