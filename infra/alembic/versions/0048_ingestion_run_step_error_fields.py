"""Add error_code / error_message on ingestion_run_step for structured failures.

Revision ID: 0048
Revises: 0047
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ingestion_run_step", sa.Column("error_code", sa.Text(), nullable=True))
    op.add_column("ingestion_run_step", sa.Column("error_message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ingestion_run_step", "error_message")
    op.drop_column("ingestion_run_step", "error_code")
