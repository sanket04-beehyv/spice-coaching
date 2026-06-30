"""Add module_candidate_draft.ingestion_instruction_rationale.

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-19

Per-candidate rationale for why Stage C emitted the module when admin
ingestion instructions were provided.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "module_candidate_draft",
        sa.Column("ingestion_instruction_rationale", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("module_candidate_draft", "ingestion_instruction_rationale")
