"""module_trigger_binding: module_family_id -> module_id.

Revision ID: 0027
Revises: 0026
Create Date: 2026-06-23

Backfill: each existing binding maps to the latest published module in its family.
Rows with no published module are deleted.

No-op when 0001 (or platform_models_schema.sql bootstrap) already created module_id.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL = text(
    """
    UPDATE module_trigger_binding mtb
    SET module_id = (
        SELECT m.id FROM module m
        WHERE m.module_family_id = mtb.module_family_id
          AND m.lifecycle_status = 'published'
        ORDER BY m.version DESC
        LIMIT 1
    )
    """
)

_DELETE_ORPHANS = text(
    "DELETE FROM module_trigger_binding WHERE module_id IS NULL"
)


def _has_module_family_id_column() -> bool:
    bind = op.get_bind()
    columns = {col["name"] for col in inspect(bind).get_columns("module_trigger_binding")}
    return "module_family_id" in columns


def upgrade() -> None:
    if not _has_module_family_id_column():
        return

    op.add_column(
        "module_trigger_binding",
        sa.Column(
            "module_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.get_bind().execute(_BACKFILL)
    op.get_bind().execute(_DELETE_ORPHANS)
    op.alter_column("module_trigger_binding", "module_id", nullable=False)
    op.drop_constraint(
        "uq_module_trigger_binding_pair",
        "module_trigger_binding",
        type_="unique",
    )
    op.drop_column("module_trigger_binding", "module_family_id")
    op.create_unique_constraint(
        "uq_module_trigger_binding_pair",
        "module_trigger_binding",
        ["module_id", "trigger_definition_id"],
    )


def downgrade() -> None:
    if _has_module_family_id_column():
        return

    op.add_column(
        "module_trigger_binding",
        sa.Column(
            "module_family_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("module_family.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.get_bind().execute(
        text(
            """
            UPDATE module_trigger_binding mtb
            SET module_family_id = (
                SELECT m.module_family_id FROM module m WHERE m.id = mtb.module_id
            )
            """
        )
    )
    op.get_bind().execute(
        text("DELETE FROM module_trigger_binding WHERE module_family_id IS NULL")
    )
    op.alter_column("module_trigger_binding", "module_family_id", nullable=False)
    op.drop_constraint(
        "uq_module_trigger_binding_pair",
        "module_trigger_binding",
        type_="unique",
    )
    op.drop_column("module_trigger_binding", "module_id")
    op.create_unique_constraint(
        "uq_module_trigger_binding_pair",
        "module_trigger_binding",
        ["module_family_id", "trigger_definition_id"],
    )
