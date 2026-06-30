"""Add bilingual candidate descriptions.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-27

Adds ``description_en`` / ``description_bn`` to ``module_candidate_draft`` so
Stage 2 (identify) can persist the LLM-generated module description in both
languages for Stage 2-draft to reuse when creating the Module row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("module_candidate_draft", sa.Column("description_en", sa.Text(), nullable=True))
    op.add_column("module_candidate_draft", sa.Column("description_bn", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("module_candidate_draft", "description_bn")
    op.drop_column("module_candidate_draft", "description_en")
