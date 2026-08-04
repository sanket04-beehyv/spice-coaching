"""Replace content_domain clinical_with_app_workflows with operational.

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-27

Allowed values become: clinical | digital | operational.
Existing clinical_with_app_workflows rows are remapped to clinical
(operational is a new distinct category, not a rename).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE source_document
        SET content_domain = 'clinical'
        WHERE content_domain = 'clinical_with_app_workflows'
        """
    )


def downgrade() -> None:
    # operational did not exist before this revision. Map it back to
    # clinical so a rollback never reintroduces a value the current code
    # no longer recognises.
    op.execute(
        """
        UPDATE source_document
        SET content_domain = 'clinical'
        WHERE content_domain = 'operational'
        """
    )
