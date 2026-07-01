#!/usr/bin/env python3
"""Enqueue embedding regeneration for all published modules.

Mirrors POST /admin/modules/{id}/regenerate-embedding: each module is handed
off to the ``platform.generate_module_embedding`` Celery task, which reads
``module_json``, calls ai-runtime ``/embed``, and writes ``module.embedding``.

Prerequisites (same env as platform service):

1. ``DATABASE_URL`` (and related platform config) so the script can query modules.
2. ``REDIS_URL`` so Celery can enqueue tasks.
3. A Celery worker running with ``platform.generate_module_embedding`` registered.
4. ai-runtime reachable from the **worker** (not from this script).

Usage:
    uv run python bin/regenerate_module_embeddings.py [--dry-run] [--missing-only]

Examples:
    # List published modules that would be enqueued
    uv run python bin/regenerate_module_embeddings.py --dry-run

    # Enqueue regeneration for every published module
    uv run python bin/regenerate_module_embeddings.py

    # Only modules that still have a NULL embedding column
    uv run python bin/regenerate_module_embeddings.py --missing-only
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from platform_service.celery_tasks import generate_module_embedding_task
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.localized import primary_text
from sqlalchemy import select


def _title_label(title_localized: dict[str, str] | None) -> str:
    return primary_text(title_localized) or ""


async def _fetch_published_modules(*, missing_only: bool) -> list[tuple[UUID, str]]:
    stmt = (
        select(Module.id, Module.title_localized)
        .where(Module.lifecycle_status == "published")
        .order_by(Module.published_at.asc().nullslast(), Module.created_at.asc())
    )
    if missing_only:
        stmt = stmt.where(Module.embedding.is_(None))
    async with SessionLocal() as session:
        result = await session.execute(stmt)
        return [(row.id, _title_label(row.title_localized)) for row in result.all()]


async def _run(*, dry_run: bool, missing_only: bool) -> int:
    modules = await _fetch_published_modules(missing_only=missing_only)
    if not modules:
        print("No published modules matched — nothing to do.")
        return 0

    if dry_run:
        print(f"Dry run: would enqueue {len(modules)} module(s).")
        for module_id, title in modules:
            print(f"  {module_id}  {title!r}")
        return 0

    enqueued = 0
    for module_id, title in modules:
        generate_module_embedding_task.delay(str(module_id))
        print(f"enqueued {module_id}  {title!r}")
        enqueued += 1
    print(f"Done. enqueued={enqueued}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print module IDs and titles without enqueueing Celery tasks",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="Only modules whose embedding column is NULL",
    )
    args = parser.parse_args()
    return asyncio.run(_run(dry_run=args.dry_run, missing_only=args.missing_only))


if __name__ == "__main__":
    raise SystemExit(main())
