"""Seed assessment-due workflow_event triggers from seed/assessment_due_triggers.json.

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-23

Idempotent upsert by trigger_code. No-op if the seed file is absent (local devs may
not have it checked in). Downgrade removes only trigger_codes from that file.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alembic import op
from sqlalchemy import text

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_PATH = _REPO_ROOT / "seed" / "assessment_due_triggers.json"

_UPSERT = text(
    """
    INSERT INTO trigger_definition (
        trigger_kind,
        trigger_code,
        description,
        predicate_jsonb,
        predicate_schema_version,
        status
    ) VALUES (
        'workflow_event',
        :trigger_code,
        :description,
        CAST(:predicate_jsonb AS jsonb),
        1,
        'active'
    )
    ON CONFLICT (trigger_code) DO UPDATE SET
        description = EXCLUDED.description,
        predicate_jsonb = EXCLUDED.predicate_jsonb,
        status = 'active',
        updated_at = now()
    """
)

_DELETE = text("DELETE FROM trigger_definition WHERE trigger_code = ANY(:codes)")


def _load_assessment_due_triggers() -> list[dict[str, Any]]:
    if not _SEED_PATH.exists():
        return []
    payload = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    triggers = payload.get("triggers", [])
    return triggers if isinstance(triggers, list) else []


def upgrade() -> None:
    triggers = _load_assessment_due_triggers()
    if not triggers:
        return
    conn = op.get_bind()
    for entry in triggers:
        conn.execute(
            _UPSERT,
            {
                "trigger_code": entry["trigger_code"],
                "description": entry.get("description", ""),
                "predicate_jsonb": json.dumps(entry.get("predicate", {})),
            },
        )


def downgrade() -> None:
    triggers = _load_assessment_due_triggers()
    if not triggers:
        return
    codes = [entry["trigger_code"] for entry in triggers]
    op.get_bind().execute(_DELETE, {"codes": codes})
