"""Move ingest config from source_document onto ingest_batch.

Revision ID: 0049
Revises: 0048
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingest_batch",
        sa.Column(
            "assessment_mode",
            sa.Text(),
            nullable=False,
            server_default="with_quiz",
        ),
    )
    op.add_column(
        "ingest_batch",
        sa.Column("ingestion_instructions", sa.Text(), nullable=True),
    )
    op.add_column(
        "ingest_batch",
        sa.Column("cards_per_module", sa.Integer(), nullable=True),
    )
    op.add_column(
        "ingest_batch",
        sa.Column("quizzes_per_module", sa.Integer(), nullable=True),
    )

    op.drop_column("source_document", "assessment_mode")
    op.drop_column("source_document", "ingestion_instructions")
    op.drop_column("source_document", "target_cards_per_module")
    op.drop_column("source_document", "target_quizzes_per_module")


def downgrade() -> None:
    op.add_column(
        "source_document",
        sa.Column(
            "assessment_mode",
            sa.Text(),
            nullable=False,
            server_default="with_quiz",
        ),
    )
    op.add_column(
        "source_document",
        sa.Column("ingestion_instructions", sa.Text(), nullable=True),
    )
    op.add_column(
        "source_document",
        sa.Column("target_cards_per_module", sa.Integer(), nullable=True),
    )
    op.add_column(
        "source_document",
        sa.Column("target_quizzes_per_module", sa.Integer(), nullable=True),
    )

    op.drop_column("ingest_batch", "quizzes_per_module")
    op.drop_column("ingest_batch", "cards_per_module")
    op.drop_column("ingest_batch", "ingestion_instructions")
    op.drop_column("ingest_batch", "assessment_mode")
