"""Create prompt_template table for DB-backed LLM prompts.

Revision ID: 0043
Revises: 0042
Create Date: 2026-07-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "prompt_template",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("template_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("variant_key", sa.Text(), nullable=True),
        sa.Column("generation_type", sa.Text(), nullable=False),
        sa.Column("system_prompt_template", sa.Text(), nullable=False),
        sa.Column("human_message_template", sa.Text(), nullable=False),
        sa.Column("required_variables", postgresql.JSONB(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("change_notes", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "template_id",
            "variant_key",
            "version",
            name="uq_prompt_template_id_variant_version",
        ),
    )
    op.create_index(
        "ix_prompt_template_template_id",
        "prompt_template",
        ["template_id"],
    )
    op.create_index(
        "ix_prompt_template_active_lookup",
        "prompt_template",
        ["template_id", "variant_key", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_prompt_template_active_lookup", table_name="prompt_template")
    op.drop_index("ix_prompt_template_template_id", table_name="prompt_template")
    op.drop_table("prompt_template")
