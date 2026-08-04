#!/usr/bin/env python3
"""Enqueue module search metadata generation for published modules missing it.

Mirrors the post-publish ``platform.generate_module_search_metadata`` Celery task:
each module is handed off to the worker, which calls ai-runtime and writes
``module.search_metadata_jsonb``. Downstream embedding regeneration is not
chained (use ``bin/regenerate_module_embeddings.py`` if needed).

After workers finish, re-run BM25 eval:

    uv run python -m eval.rag --k 5 --output eval/rag/reports/bm25-metadata-v1.json

Prerequisites (same env as platform service):

1. ``DATABASE_URL`` (and related platform config) so the script can query modules.
2. ``REDIS_URL`` so Celery can enqueue tasks.
3. A Celery worker running with ``platform.generate_module_search_metadata`` registered.
4. ai-runtime reachable from the **worker** (not from this script).

Usage:
    uv run python bin/backfill_module_search_metadata.py [--dry-run] [--missing-only]
    uv run python bin/backfill_module_search_metadata.py --module-id <uuid>

Examples:
    # List published modules that would be enqueued
    uv run python bin/backfill_module_search_metadata.py --dry-run

    # Enqueue backfill for every published module missing search_metadata_jsonb
    uv run python bin/backfill_module_search_metadata.py --missing-only

    # Single module (any lifecycle status)
    uv run python bin/backfill_module_search_metadata.py --module-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from platform_service.celery_tasks import generate_module_search_metadata_task
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_read_repository import ModuleReadRepository
from platform_service.localized import primary_text
from sqlalchemy import select

ModuleRow = tuple[UUID, str]


def _title_label(title_localized: dict[str, str] | None) -> str:
    return primary_text(title_localized) or ""


async def _fetch_published_modules(
    *,
    missing_only: bool,
    tenant_id: UUID | None,
) -> list[ModuleRow]:
    repo = ModuleReadRepository()
    async with SessionLocal() as session:
        repo._session = session
        rows = await repo.list_modules(status="published", limit=10_000, tenant_id=tenant_id)
        modules = [(m.id, _title_label(m.title_localized)) for m in rows]

        if not missing_only:
            return modules

        out: list[ModuleRow] = []
        for mid, title in modules:
            mod = await session.get(Module, mid)
            if mod is None or mod.search_metadata_jsonb:
                continue
            out.append((mid, title))
        return out


async def _fetch_module_by_id(module_id: UUID) -> ModuleRow | None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Module.id, Module.title_localized).where(Module.id == module_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return (row.id, _title_label(row.title_localized))


async def _run(
    *,
    dry_run: bool,
    missing_only: bool,
    tenant_id: UUID | None,
    module_id: UUID | None,
) -> int:
    if module_id is not None:
        module = await _fetch_module_by_id(module_id)
        if module is None:
            print(f"Module {module_id} not found.", file=sys.stderr)
            return 1
        if missing_only:
            async with SessionLocal() as session:
                mod = await session.get(Module, module_id)
                if mod is None or mod.search_metadata_jsonb:
                    print("No modules matched — nothing to do.")
                    return 0
        modules = [module]
    else:
        modules = await _fetch_published_modules(missing_only=missing_only, tenant_id=tenant_id)

    if not modules:
        print("No modules matched — nothing to do.")
        return 0

    if dry_run:
        print(f"Dry run: would enqueue {len(modules)} module(s).")
        for mid, title in modules:
            print(f"  {mid}  {title!r}")
        return 0

    enqueued = 0
    for mid, title in modules:
        generate_module_search_metadata_task.delay(str(mid), chain_downstream=False)
        print(f"enqueued {mid}  {title!r}")
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
        help="Only modules whose search_metadata_jsonb column is NULL or empty",
    )
    parser.add_argument(
        "--tenant-id",
        type=UUID,
        default=None,
        help="Restrict bulk backfill to one tenant",
    )
    parser.add_argument(
        "--module-id",
        type=UUID,
        default=None,
        help="Backfill a single module by ID (skips bulk published-module query)",
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(
            dry_run=args.dry_run,
            missing_only=args.missing_only,
            tenant_id=args.tenant_id,
            module_id=args.module_id,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
