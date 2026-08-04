"""Seed prompt_template rows from seed/prompt_templates.json.

Revision ID: 0044
Revises: 0043
Create Date: 2026-07-21
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_PATH = Path(__file__).resolve().parents[3] / "seed" / "prompt_templates.json"


def upgrade() -> None:
    if not _SEED_PATH.exists():
        raise RuntimeError(f"seed file not found: {_SEED_PATH}")

    rows = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    bind = op.get_bind()
    for row in rows:
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
                ON CONFLICT ON CONSTRAINT uq_prompt_template_id_variant_version DO NOTHING
                """
            ),
            {
                "id": uuid.UUID(row["id"]),
                "template_id": row["template_id"],
                "version": row["version"],
                "variant_key": row.get("variant_key"),
                "generation_type": row["generation_type"],
                "system_prompt_template": row["system_prompt_template"],
                "human_message_template": row["human_message_template"],
                "required_variables": json.dumps(row.get("required_variables") or []),
                "title": row.get("title"),
                "description": row.get("description"),
                "change_notes": row.get("change_notes"),
                "status": row.get("status", "active"),
            },
        )


def downgrade() -> None:
    if not _SEED_PATH.exists():
        return
    rows = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    ids = [str(uuid.UUID(row["id"])) for row in rows]
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM prompt_template WHERE id = ANY(CAST(:ids AS uuid[]))"),
        {"ids": ids},
    )
