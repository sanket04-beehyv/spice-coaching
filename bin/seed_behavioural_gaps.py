#!/usr/bin/env python3
"""Seed (or refresh) the behavioural_gap registry from a JSON file.

Idempotent — upserts by `gap_code`. Existing gaps with the same code have
their `description`, `domain`, and `severity_default` updated; new ones are
inserted. Nothing is deleted.

Usage:
    uv run python bin/seed_behavioural_gaps.py [--file PATH]

Defaults to seed/behavioural_gaps_pilot.json relative to the repo root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED = REPO_ROOT / "seed" / "behavioural_gaps_pilot.json"


async def _seed(path: Path) -> None:
    # Local import so module discovery works from project root.
    from platform_service.db.base import SessionLocal
    from platform_service.db.models.behavioural_gap import BehaviouralGap

    payload = json.loads(path.read_text(encoding="utf-8"))
    gaps = payload.get("gaps", [])
    if not gaps:
        print("No gaps in seed file — nothing to do.")
        return

    inserted = 0
    updated = 0
    async with SessionLocal() as session:  # type: AsyncSession
        for entry in gaps:
            code = entry["gap_code"]
            result = await session.execute(select(BehaviouralGap).where(BehaviouralGap.gap_code == code))
            existing = result.scalar_one_or_none()
            if existing is None:
                session.add(
                    BehaviouralGap(
                        gap_code=code,
                        description=entry["description"],
                        domain=entry["domain"],
                        severity_default=entry.get("severity_default", "moderate"),
                        detection_rule_jsonb=entry.get("detection_rule_jsonb", {}),
                        status="active",
                    )
                )
                inserted += 1
            else:
                existing.description = entry["description"]
                existing.domain = entry["domain"]
                existing.severity_default = entry.get("severity_default", existing.severity_default)
                if "detection_rule_jsonb" in entry:
                    existing.detection_rule_jsonb = entry["detection_rule_jsonb"]
                # Re-activate if previously deprecated; explicit deprecation is a
                # deliberate operator action, not a side effect of re-seeding.
                existing.status = "active"
                updated += 1
        await session.commit()
    print(f"Seed complete. Inserted={inserted} updated={updated} total={len(gaps)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        default=DEFAULT_SEED,
        help=f"Path to seed JSON (default: {DEFAULT_SEED})",
    )
    args = parser.parse_args()
    if not args.file.exists():
        print(f"Seed file not found: {args.file}", file=sys.stderr)
        return 1
    asyncio.run(_seed(args.file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
