"""Add source_document cardinality targets for ingest steering.

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-06

Optional admin-provided fixed card/quiz counts per module, set at ingest
and threaded through Stage C/D and post-publish quiz generation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_document",
        sa.Column("target_cards_per_module", sa.Integer(), nullable=True),
    )
    op.add_column(
        "source_document",
        sa.Column("target_quizzes_per_module", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("source_document", "target_quizzes_per_module")
    op.drop_column("source_document", "target_cards_per_module")
