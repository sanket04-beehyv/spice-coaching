#!/usr/bin/env python3
"""Enqueue card search metadata generation for published modules.

Mirrors the post-publish ``platform.generate_module_card_search_metadata_batch``
Celery task: each module is handed off to the worker, which calls ai-runtime and
writes ``module_card.search_metadata_jsonb``. Downstream module metadata and
embedding regeneration are not chained.

After workers finish, re-run BM25 card eval:

    uv run python -m eval.rag --k 5 --output eval/rag/reports/bm25-card-metadata-v1.json

Prerequisites (same env as platform service):

1. ``DATABASE_URL`` (and related platform config) so the script can query modules.
2. ``REDIS_URL`` so Celery can enqueue tasks.
3. A Celery worker running with ``platform.generate_module_card_search_metadata_batch``
   registered.
4. ai-runtime reachable from the **worker** (not from this script).

Usage:
    uv run python bin/backfill_card_search_metadata.py [--dry-run] [--missing-only]
    uv run python bin/backfill_card_search_metadata.py --module-id <uuid>

Examples:
    # List modules/cards that would be enqueued (no LLM calls)
    uv run python bin/backfill_card_search_metadata.py --dry-run

    # Enqueue backfill for cards missing search_metadata on every published module
    uv run python bin/backfill_card_search_metadata.py --missing-only

    # Single module (any lifecycle status)
    uv run python bin/backfill_card_search_metadata.py --module-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from platform_service.celery_tasks import generate_module_card_search_metadata_batch_task
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.repositories.module_read_repository import ModuleReadRepository
from platform_service.localized import primary_text
from platform_service.services.card_normalisation import card_row_to_dict

ModuleTarget = tuple[UUID, int, str]


def _title_label(title_localized: dict[str, str] | None) -> str:
    return primary_text(title_localized) or ""


async def _fetch_module_targets(
    *,
    missing_only: bool,
    tenant_id: UUID | None,
    module_id: UUID | None,
) -> list[ModuleTarget]:
    async with SessionLocal() as session:
        if module_id is not None:
            module = await session.get(Module, module_id)
            modules = [module] if module is not None else []
        else:
            repo = ModuleReadRepository()
            repo._session = session
            modules = await repo.list_modules(status="published", limit=10_000, tenant_id=tenant_id)

        targets: list[ModuleTarget] = []
        for module in modules:
            if module is None:
                continue
            card_rows = await ModuleReadRepository(session).list_cards(module.id)
            card_count = 0
            for row in card_rows:
                card = card_row_to_dict(row)
                if missing_only and card.get("search_metadata"):
                    continue
                card_count += 1
            if card_count:
                targets.append((module.id, card_count, _title_label(module.title_localized)))
        return targets


async def _run(
    *,
    dry_run: bool,
    missing_only: bool,
    tenant_id: UUID | None,
    module_id: UUID | None,
) -> int:
    targets = await _fetch_module_targets(
        missing_only=missing_only,
        tenant_id=tenant_id,
        module_id=module_id,
    )
    if module_id is not None and not targets:
        async with SessionLocal() as session:
            module = await session.get(Module, module_id)
            if module is None:
                print(f"Module {module_id} not found.", file=sys.stderr)
                return 1
        print("No cards matched — nothing to do.")
        return 0

    if not targets:
        print("No cards matched — nothing to do.")
        return 0

    total_cards = sum(card_count for _, card_count, _ in targets)

    if dry_run:
        print(f"Dry run: would enqueue {len(targets)} module(s) ({total_cards} card(s)).")
        for mid, card_count, title in targets:
            print(f"  {mid}  {card_count} card(s)  {title!r}")
        return 0

    enqueued = 0
    force = not missing_only
    for mid, card_count, title in targets:
        generate_module_card_search_metadata_batch_task.delay(
            str(mid),
            force=force,
            chain_downstream=False,
        )
        print(f"enqueued {mid}  {card_count} card(s)  {title!r}")
        enqueued += 1
    print(f"Done. enqueued={enqueued}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print module/card targets without enqueueing Celery tasks",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="Only cards that do not yet have search_metadata",
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
        help="Backfill cards for a single module by ID (skips bulk published-module query)",
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
