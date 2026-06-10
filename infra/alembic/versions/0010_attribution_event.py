"""attribution_event — append-only audit trail.

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-23

Tracks the source/ingest/module lifecycle: upload finalised, ingest
started, ingest completed/failed, module published, module retired,
clinically reviewed flag set, source viewed. ``actor`` is the
``AdminCaller.subject`` (client-asserted today; OIDC later — see
``auth.admin_access``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attribution_event",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("module_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("payload_jsonb", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_attribution_event_source_document",
        "attribution_event",
        ["source_document_id"],
    )
    op.create_index("ix_attribution_event_event_type", "attribution_event", ["event_type"])


def downgrade() -> None:
    op.drop_index("ix_attribution_event_event_type", table_name="attribution_event")
    op.drop_index("ix_attribution_event_source_document", table_name="attribution_event")
    op.drop_table("attribution_event")
