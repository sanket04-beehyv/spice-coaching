"""Create chw_video_assignment and chw_video_progress tables.

Revision ID: 0051
Revises: 0050
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chw_video_assignment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_document_id", sa.UUID(), nullable=False),
        sa.Column("assignment_type", sa.String(length=50), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("upazila", sa.String(length=100), nullable=True),
        sa.Column("assigned_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_document.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_document_id",
            "tenant_id",
            name="uq_video_assignment_tenant",
        ),
        sa.UniqueConstraint(
            "source_document_id",
            "user_id",
            name="uq_video_assignment_user",
        ),
        sa.UniqueConstraint(
            "source_document_id",
            "upazila",
            name="uq_video_assignment_upazila",
        ),
    )
    op.create_index(
        op.f("ix_chw_video_assignment_tenant_id"),
        "chw_video_assignment",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chw_video_assignment_user_id"),
        "chw_video_assignment",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chw_video_assignment_upazila"),
        "chw_video_assignment",
        ["upazila"],
        unique=False,
    )

    op.create_table(
        "chw_video_progress",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("chw_id", sa.BigInteger(), nullable=False),
        sa.Column("source_document_id", sa.UUID(), nullable=False),
        sa.Column("last_position_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("percent_watched", sa.Float(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "last_watched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_document.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chw_id",
            "source_document_id",
            name="uq_video_progress_chw_video",
        ),
    )
    op.create_index(
        op.f("ix_chw_video_progress_chw_id"),
        "chw_video_progress",
        ["chw_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_chw_video_progress_source_document_id"),
        "chw_video_progress",
        ["source_document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chw_video_progress_source_document_id"), table_name="chw_video_progress")
    op.drop_index(op.f("ix_chw_video_progress_chw_id"), table_name="chw_video_progress")
    op.drop_table("chw_video_progress")
    op.drop_index(op.f("ix_chw_video_assignment_upazila"), table_name="chw_video_assignment")
    op.drop_index(op.f("ix_chw_video_assignment_user_id"), table_name="chw_video_assignment")
    op.drop_index(op.f("ix_chw_video_assignment_tenant_id"), table_name="chw_video_assignment")
    op.drop_table("chw_video_assignment")
