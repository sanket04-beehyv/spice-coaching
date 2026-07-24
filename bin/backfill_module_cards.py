#!/usr/bin/env -S uv run python
"""Migrate legacy ``module_json.cards`` into relational ``module_card`` rows.

Use when inline cards remain in ``module.module_json`` after deploying the
module-card normalization (Alembic 0033). Mirrors the data pass in
``infra/alembic/versions/0033_module_card.py`` but runs through platform
service code (``ModuleCardService`` + ``card_dict_to_row_fields``).

Prerequisites:
- ``DATABASE_URL`` configured (same env as platform service).
- ``module_card`` table exists (``alembic upgrade head``).

Usage:
    uv run python bin/backfill_module_cards.py --dry-run
    uv run python bin/backfill_module_cards.py
    uv run python bin/backfill_module_cards.py --module-id <uuid>

Idempotent behaviour:
- Skips modules with no legacy ``cards`` key (or empty array).
- If ``module_card`` rows already exist for a module, only strips ``cards``
  from ``module_json`` (repair pass).
- Otherwise inserts rows (minting ``card_family_id`` when absent) then strips.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from uuid import UUID

from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.models.module_card import ModuleCard
from platform_service.localized import primary_text
from platform_service.services.module_card_service import (
    ModuleCardService,
    extract_cards_from_module_json,
    module_json_shell,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationTarget:
    module_id: UUID
    card_count: int
    title: str
    existing_card_rows: int


async def _table_exists(session: AsyncSession, table: str) -> bool:
    row = (
        await session.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t"),
            {"t": table},
        )
    ).first()
    return row is not None


def _legacy_card_count(module_json: dict | None) -> int:
    if not isinstance(module_json, dict):
        return 0
    cards = module_json.get("cards")
    if not isinstance(cards, list):
        return 0
    return sum(1 for card in cards if isinstance(card, dict))


async def _existing_card_row_count(session: AsyncSession, module_id: UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(ModuleCard).where(ModuleCard.module_id == module_id)
    )
    return int(result.scalar_one())


async def _fetch_targets(
    session: AsyncSession,
    *,
    module_id: UUID | None,
) -> list[MigrationTarget]:
    if module_id is not None:
        module = await session.get(Module, module_id)
        modules = [module] if module is not None else []
    else:
        result = await session.execute(
            select(Module).where(Module.module_json.isnot(None)).order_by(Module.created_at.asc())
        )
        modules = list(result.scalars().all())

    targets: list[MigrationTarget] = []
    for module in modules:
        if module is None:
            continue
        legacy_count = _legacy_card_count(module.module_json)
        if legacy_count == 0:
            continue
        row_count = await _existing_card_row_count(session, module.id)
        targets.append(
            MigrationTarget(
                module_id=module.id,
                card_count=legacy_count,
                title=primary_text(module.title_localized) or "",
                existing_card_rows=row_count,
            )
        )
    return targets


async def _migrate_module(
    session: AsyncSession,
    module: Module,
    *,
    dry_run: bool,
) -> tuple[int, bool]:
    """Return (cards_inserted, json_stripped)."""
    cards = extract_cards_from_module_json(module.module_json)
    if not cards:
        return 0, False

    existing_rows = await _existing_card_row_count(session, module.id)
    inserted = 0
    if existing_rows == 0:
        if dry_run:
            inserted = len(cards)
        else:
            await ModuleCardService(session).append_cards(module.id, cards)
            await session.flush()
            inserted = await _existing_card_row_count(session, module.id)

    shell = module_json_shell(module.module_json)
    if dry_run:
        return inserted, True

    module.module_json = shell
    await session.flush()
    return inserted, True


async def _run(*, dry_run: bool, module_id: UUID | None) -> int:
    async with SessionLocal() as session:
        if not await _table_exists(session, "module_card"):
            logger.error("module_card table not found — run `alembic upgrade head` first")
            return 1

        targets = await _fetch_targets(session, module_id=module_id)
        if module_id is not None and not targets:
            module = await session.get(Module, module_id)
            if module is None:
                logger.error("module %s not found", module_id)
                return 1
            logger.info("module %s has no legacy module_json.cards — nothing to do", module_id)
            return 0

        if not targets:
            logger.info("no modules with legacy module_json.cards — nothing to do")
            return 0

        if dry_run:
            logger.info("dry run: would process %d module(s)", len(targets))
            for target in targets:
                action = (
                    "strip cards key only"
                    if target.existing_card_rows > 0
                    else f"insert {target.card_count} card row(s) and strip"
                )
                logger.info(
                    "  %s  legacy_cards=%d  existing_rows=%d  %r  -> %s",
                    target.module_id,
                    target.card_count,
                    target.existing_card_rows,
                    target.title,
                    action,
                )
            return 0

        migrated_modules = 0
        inserted_total = 0
        stripped_total = 0
        for target in targets:
            module = await session.get(Module, target.module_id)
            if module is None:
                continue
            inserted, stripped = await _migrate_module(session, module, dry_run=False)
            migrated_modules += 1
            inserted_total += inserted
            stripped_total += int(stripped)
            logger.info(
                "migrated %s  inserted_rows=%d  stripped_json=%s  %r",
                target.module_id,
                inserted,
                stripped,
                target.title,
            )

        await session.commit()
        logger.info(
            "done modules=%d cards_inserted=%d json_stripped=%d",
            migrated_modules,
            inserted_total,
            stripped_total,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List modules that would be migrated without writing",
    )
    parser.add_argument(
        "--module-id",
        type=UUID,
        default=None,
        help="Migrate a single module by id",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_run(dry_run=args.dry_run, module_id=args.module_id))


if __name__ == "__main__":
    raise SystemExit(main())
