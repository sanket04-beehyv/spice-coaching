"""Add chatbot_faqs_only flag on module.

Revision ID: 0039
Revises: 0038
Create Date: 2026-07-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    module_columns = {col["name"] for col in inspector.get_columns("module")}
    if "chatbot_faqs_only" not in module_columns:
        op.add_column(
            "module",
            sa.Column(
                "chatbot_faqs_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    family_columns = {col["name"] for col in inspector.get_columns("module_family")}
    if "chatbot_faqs_only" in family_columns:
        op.drop_column("module_family", "chatbot_faqs_only")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    module_columns = {col["name"] for col in inspector.get_columns("module")}
    if "chatbot_faqs_only" in module_columns:
        op.drop_column("module", "chatbot_faqs_only")

    family_columns = {col["name"] for col in inspector.get_columns("module_family")}
    if "chatbot_faqs_only" not in family_columns:
        op.add_column(
            "module_family",
            sa.Column(
                "chatbot_faqs_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
