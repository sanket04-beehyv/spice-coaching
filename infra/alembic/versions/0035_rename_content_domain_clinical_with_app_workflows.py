"""Rename content_domain clinical_with_app_action to clinical_with_app_workflows.

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE source_document
        SET content_domain = 'clinical_with_app_workflows'
        WHERE content_domain = 'clinical_with_app_action'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE source_document
        SET content_domain = 'clinical_with_app_action'
        WHERE content_domain = 'clinical_with_app_workflows'
        """
    )
