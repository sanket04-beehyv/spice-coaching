"""Create module_creation_suggestion tables and seed prompt template.

Revision ID: 0053
Revises: 0052
Create Date: 2026-07-30
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_PATH = Path(__file__).resolve().parents[3] / "seed" / "prompt_templates.json"
_TEMPLATE_ID = "module-creation-suggestion"


def upgrade() -> None:
    op.create_table(
        "module_creation_suggestion",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("suggestion_date", sa.Date(), nullable=False),
        sa.Column("suggestion_kind", sa.Text(), nullable=False),
        sa.Column("matched_module_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("proposed_topic", sa.Text(), nullable=True),
        sa.Column("display_title", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("question_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["matched_module_id"], ["module.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_module_creation_suggestion_tenant_date",
        "module_creation_suggestion",
        ["tenant_id", "suggestion_date"],
    )
    op.create_index(
        "ix_module_creation_suggestion_date",
        "module_creation_suggestion",
        ["suggestion_date"],
    )
    op.create_index(
        "ix_module_creation_suggestion_matched_module",
        "module_creation_suggestion",
        ["matched_module_id"],
    )

    op.create_table(
        "module_creation_suggestion_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("suggestion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_event_id", sa.Text(), nullable=True),
        sa.Column("sample_chw_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ["suggestion_id"],
            ["module_creation_suggestion.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_module_creation_suggestion_evidence_suggestion",
        "module_creation_suggestion_evidence",
        ["suggestion_id"],
    )

    _seed_prompt_template()


def _seed_prompt_template() -> None:
    if not _SEED_PATH.exists():
        return
    rows = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    row = next((r for r in rows if r.get("template_id") == _TEMPLATE_ID), None)
    if row is None:
        return
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            """
            SELECT 1 FROM prompt_template
            WHERE template_id = :template_id
              AND version = :version
              AND variant_key IS NULL
            LIMIT 1
            """
        ),
        {"template_id": row["template_id"], "version": int(row["version"])},
    ).first()
    if existing is not None:
        return
    bind.execute(
        sa.text(
            """
            INSERT INTO prompt_template (
                id,
                template_id,
                version,
                variant_key,
                generation_type,
                system_prompt_template,
                human_message_template,
                required_variables,
                title,
                description,
                change_notes,
                status
            ) VALUES (
                :id,
                :template_id,
                :version,
                :variant_key,
                :generation_type,
                :system_prompt_template,
                :human_message_template,
                CAST(:required_variables AS jsonb),
                :title,
                :description,
                :change_notes,
                :status
            )
            """
        ),
        {
            "id": uuid.UUID(str(row["id"])),
            "template_id": row["template_id"],
            "version": int(row["version"]),
            "variant_key": row.get("variant_key"),
            "generation_type": row["generation_type"],
            "system_prompt_template": row["system_prompt_template"],
            "human_message_template": row["human_message_template"],
            "required_variables": json.dumps(row["required_variables"]),
            "title": row["title"],
            "description": row["description"],
            "change_notes": row["change_notes"],
            "status": row["status"],
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM prompt_template WHERE template_id = :template_id"),
        {"template_id": _TEMPLATE_ID},
    )
    op.drop_index(
        "ix_module_creation_suggestion_evidence_suggestion",
        table_name="module_creation_suggestion_evidence",
    )
    op.drop_table("module_creation_suggestion_evidence")
    op.drop_index(
        "ix_module_creation_suggestion_matched_module",
        table_name="module_creation_suggestion",
    )
    op.drop_index("ix_module_creation_suggestion_date", table_name="module_creation_suggestion")
    op.drop_index(
        "ix_module_creation_suggestion_tenant_date",
        table_name="module_creation_suggestion",
    )
    op.drop_table("module_creation_suggestion")
