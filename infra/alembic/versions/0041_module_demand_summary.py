"""Create module_demand_summary snapshot table (daily-refreshed admin demand).

Revision ID: 0041
Revises: 0040
Create Date: 2026-07-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "module_demand_summary",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_module_demand_summary_tenant",
        "module_demand_summary",
        ["tenant_id"],
    )
    # One snapshot per tenant scope; separate partial indexes handle the
    # global (NULL tenant) row, since Postgres treats NULLs as distinct.
    op.create_index(
        "uq_module_demand_summary_tenant",
        "module_demand_summary",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )
    op.create_index(
        "uq_module_demand_summary_global",
        "module_demand_summary",
        [sa.text("(tenant_id IS NULL)")],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_module_demand_summary_global", table_name="module_demand_summary")
    op.drop_index("uq_module_demand_summary_tenant", table_name="module_demand_summary")
    op.drop_index("ix_module_demand_summary_tenant", table_name="module_demand_summary")
    op.drop_table("module_demand_summary")
