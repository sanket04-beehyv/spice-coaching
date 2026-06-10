"""source_page timecodes for AV chunk provenance.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-23

Adds ``start_ms`` / ``end_ms`` to ``source_page`` so the AV ingestion
path can record deterministic chunk-window timecodes (owned by the
server-side splitter, never by the LLM). Both columns NULL for
PDF/DOCX/PPTX pages.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_page", sa.Column("start_ms", sa.Integer(), nullable=True))
    op.add_column("source_page", sa.Column("end_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_page", "end_ms")
    op.drop_column("source_page", "start_ms")
