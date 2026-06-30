"""Add module.search_metadata_jsonb for lexical retrieval enrichment.

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-16

LLM-generated bilingual keywords, search phrases, and topic tags used by
module_text_for_search() for BM25 eval and embedding retrieval.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "module",
        sa.Column("search_metadata_jsonb", JSONB(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("module", "search_metadata_jsonb")
