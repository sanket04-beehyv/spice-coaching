#!/usr/bin/env python3
"""Seed (or refresh) prompt_template rows from seed/prompt_templates.json.

Idempotent — upserts by (template_id, variant_key, version). Existing rows
with the same key have their prompt body and metadata updated; new ones are
inserted with the stable seed UUID. Nothing is deleted.

Usage:
    uv run python bin/seed_prompt_templates.py [--file PATH]

Defaults to seed/prompt_templates.json relative to the repo root.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from platform_service.db.base import SessionLocal
from platform_service.db.models.prompt_template import PromptTemplate
from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SEED = REPO_ROOT / "seed" / "prompt_templates.json"


def _variant_filter(variant_key: str | None):
    if variant_key is None:
        return PromptTemplate.variant_key.is_(None)
    return PromptTemplate.variant_key == variant_key


async def _seed(path: Path) -> None:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not rows:
        print("No prompt templates in seed file — nothing to do.")
        return

    inserted = 0
    updated = 0
    async with SessionLocal() as session:
        for entry in rows:
            template_id = entry["template_id"]
            version = int(entry["version"])
            variant_key = entry.get("variant_key")
            result = await session.execute(
                select(PromptTemplate).where(
                    PromptTemplate.template_id == template_id,
                    PromptTemplate.version == version,
                    _variant_filter(variant_key),
                )
            )
            existing = result.scalar_one_or_none()
            required_variables = list(entry.get("required_variables") or [])
            status = entry.get("status", "active")
            if existing is None:
                session.add(
                    PromptTemplate(
                        id=uuid.UUID(entry["id"]),
                        template_id=template_id,
                        version=version,
                        variant_key=variant_key,
                        generation_type=entry["generation_type"],
                        system_prompt_template=entry["system_prompt_template"],
                        human_message_template=entry["human_message_template"],
                        required_variables=required_variables,
                        title=entry.get("title"),
                        description=entry.get("description"),
                        change_notes=entry.get("change_notes"),
                        status=status,
                    )
                )
                inserted += 1
            else:
                existing.generation_type = entry["generation_type"]
                existing.system_prompt_template = entry["system_prompt_template"]
                existing.human_message_template = entry["human_message_template"]
                existing.required_variables = required_variables
                existing.title = entry.get("title")
                existing.description = entry.get("description")
                existing.change_notes = entry.get("change_notes")
                existing.status = status
                updated += 1
        await session.commit()
    print(f"Seed complete. Inserted={inserted} updated={updated} total={len(rows)}")


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
