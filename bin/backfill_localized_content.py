#!/usr/bin/env -S uv run python
"""Backfill locale-keyed maps in module_json cards and search_metadata_jsonb.

Run after deploying migration 0030 if any rows were inserted between the
schema cutover and application code update. Safe to re-run (idempotent on
already-migrated cards).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from platform_service.config import get_settings
from platform_service.localized import migrate_legacy_module_json, migrate_legacy_search_metadata
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)


async def _backfill(session: AsyncSession, *, dry_run: bool) -> int:
    result = await session.execute(text("SELECT id, module_json, search_metadata_jsonb FROM module"))
    rows = result.fetchall()
    updated = 0
    for module_id, module_json, search_metadata in rows:
        new_module_json = migrate_legacy_module_json(module_json)
        new_search_metadata = (
            migrate_legacy_search_metadata(search_metadata)
            if isinstance(search_metadata, dict)
            else search_metadata
        )
        if new_module_json == module_json and new_search_metadata == search_metadata:
            continue
        updated += 1
        if dry_run:
            logger.info("would update module %s", module_id)
            continue
        await session.execute(
            text(
                "UPDATE module SET module_json = CAST(:module_json AS jsonb), "
                "search_metadata_jsonb = CAST(:search_metadata AS jsonb) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {
                "id": str(module_id),
                "module_json": json.dumps(new_module_json) if new_module_json is not None else None,
                "search_metadata": json.dumps(new_search_metadata)
                if new_search_metadata is not None
                else None,
            },
        )
    if not dry_run:
        await session.commit()
    return updated


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        count = await _backfill(session, dry_run=args.dry_run)
    await engine.dispose()
    logger.info("updated %d module(s)", count)


if __name__ == "__main__":
    asyncio.run(main())
