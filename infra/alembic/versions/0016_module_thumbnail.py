"""Add module.thumbnail_storage_path for module previews.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-04

Nullable MinIO path to a module thumbnail PNG. Defaults from the first linked
source_document thumbnail at pipeline creation; admins may override via PUT.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("module", sa.Column("thumbnail_storage_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("module", "thumbnail_storage_path")
