"""Replace source_document.authority_kind with content_domain + assessment_mode.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-19

Backfills from legacy authority_kind values, then drops authority_kind.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "source_document",
        sa.Column("content_domain", sa.Text(), nullable=False, server_default="clinical"),
    )
    op.add_column(
        "source_document",
        sa.Column("assessment_mode", sa.Text(), nullable=False, server_default="with_quiz"),
    )

    op.execute(
        """
        UPDATE source_document SET content_domain = CASE authority_kind
            WHEN 'spice_digital' THEN 'digital'
            WHEN 'digital_proficiency' THEN 'digital'
            WHEN 'supervisor_update' THEN 'supervisor_update'
            WHEN 'content_update' THEN 'supervisor_update'
            ELSE 'clinical'
        END
        """
    )

    op.drop_column("source_document", "authority_kind")


def downgrade() -> None:
    op.add_column(
        "source_document",
        sa.Column(
            "authority_kind",
            sa.Text(),
            nullable=False,
            server_default="official_training",
        ),
    )

    op.execute(
        """
        UPDATE source_document SET authority_kind = CASE content_domain
            WHEN 'digital' THEN 'digital_proficiency'
            WHEN 'supervisor_update' THEN 'supervisor_update'
            ELSE 'official_training'
        END
        """
    )

    op.drop_column("source_document", "assessment_mode")
    op.drop_column("source_document", "content_domain")
