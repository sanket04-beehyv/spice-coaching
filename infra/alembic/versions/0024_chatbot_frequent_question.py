"""Add chat_frequent_question for nightly FAQ mining.

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-17

Stores ranked frequent questions mined from ClickHouse ``digital_help_used``
events for device sync via ``GET /sync/chatbot-faqs``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_frequent_question",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("question_bn", sa.Text(), nullable=False),
        sa.Column("question_en", sa.Text(), nullable=False),
        sa.Column("normalized_question", sa.Text(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "normalized_question", name="uq_chat_faq_tenant_question"),
    )
    op.create_index(
        "ix_chat_faq_tenant_rank",
        "chat_frequent_question",
        ["tenant_id", "rank"],
    )
    op.create_index(
        "ix_chat_faq_tenant_updated",
        "chat_frequent_question",
        ["tenant_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_faq_tenant_updated", table_name="chat_frequent_question")
    op.drop_index("ix_chat_faq_tenant_rank", table_name="chat_frequent_question")
    op.drop_table("chat_frequent_question")
