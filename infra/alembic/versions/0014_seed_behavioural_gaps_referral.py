"""Seed referral behavioural_gap rows from seed/behavioural_gaps_referral.json.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-04

Idempotent upsert by gap_code. No-op if the seed file is absent (local devs may
not have it checked in). Downgrade removes only gap_codes from that file.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alembic import op
from sqlalchemy import text

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SEED_PATH = _REPO_ROOT / "seed" / "behavioural_gaps_referral.json"

_UPSERT = text(
    """
    INSERT INTO behavioural_gap (
        gap_code, description, domain, severity_default, detection_rule_jsonb, status
    ) VALUES (
        :gap_code,
        :description,
        :domain,
        :severity_default,
        CAST(:detection_rule_jsonb AS jsonb),
        'active'
    )
    ON CONFLICT (gap_code) DO UPDATE SET
        description = EXCLUDED.description,
        domain = EXCLUDED.domain,
        severity_default = EXCLUDED.severity_default,
        detection_rule_jsonb = EXCLUDED.detection_rule_jsonb,
        status = 'active',
        updated_at = now()
    """
)

_DELETE = text("DELETE FROM behavioural_gap WHERE gap_code = ANY(:codes)")


def _load_referral_gaps() -> list[dict[str, Any]]:
    if not _SEED_PATH.exists():
        return []
    payload = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    gaps = payload.get("gaps", [])
    return gaps if isinstance(gaps, list) else []


def upgrade() -> None:
    gaps = _load_referral_gaps()
    if not gaps:
        return
    conn = op.get_bind()
    for entry in gaps:
        conn.execute(
            _UPSERT,
            {
                "gap_code": entry["gap_code"],
                "description": entry["description"],
                "domain": entry["domain"],
                "severity_default": entry.get("severity_default", "moderate"),
                "detection_rule_jsonb": json.dumps(entry.get("detection_rule_jsonb", {})),
            },
        )


def downgrade() -> None:
    gaps = _load_referral_gaps()
    if not gaps:
        return
    codes = [entry["gap_code"] for entry in gaps]
    op.get_bind().execute(_DELETE, {"codes": codes})
