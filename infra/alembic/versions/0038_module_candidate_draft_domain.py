"""Add module_candidate_draft.domain for Stage C → Stage D handoff.

Revision ID: 0038
Revises: 0037
Create Date: 2026-07-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("module_candidate_draft", sa.Column("domain", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("module_candidate_draft", "domain")
