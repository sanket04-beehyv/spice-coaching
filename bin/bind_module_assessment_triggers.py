#!/usr/bin/env python3
"""Enqueue assessment-due trigger binding for published modules.

Mirrors POST /admin/modules/{id}/bind-assessment-triggers: each module is
handed off to the ``platform.bind_assessment_triggers`` Celery task, which
classifies assessment topics and writes ``module_trigger_binding`` rows at the
module level.

Prerequisites (same env as platform service):

1. ``DATABASE_URL`` (and related platform config) so the script can query modules.
2. ``REDIS_URL`` so Celery can enqueue tasks.
3. A Celery worker running with ``platform.bind_assessment_triggers`` registered.
4. ai-runtime reachable from the **worker** (not from this script); metadata
   rules are used when the classifier LLM call fails.
5. Assessment-due triggers seeded (migration ``0025_seed_assessment_due_triggers``).

Usage:
    uv run python bin/bind_module_assessment_triggers.py [--dry-run] [--missing-only]
    uv run python bin/bind_module_assessment_triggers.py --module-id <uuid>

Examples:
    # List published modules that would be enqueued
    uv run python bin/bind_module_assessment_triggers.py --dry-run

    # Enqueue binding for every published module
    uv run python bin/bind_module_assessment_triggers.py

    # Only published modules with no assessment-due binding
    uv run python bin/bind_module_assessment_triggers.py --missing-only

    # Single module (any lifecycle status)
    uv run python bin/bind_module_assessment_triggers.py --module-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from platform_service.celery_tasks import bind_assessment_triggers_task
from platform_service.db.base import SessionLocal
from platform_service.db.models.module import Module
from platform_service.db.models.trigger_definition import ModuleTriggerBinding, TriggerDefinition
from platform_service.localized import primary_text
from sqlalchemy import exists, select

ModuleRow = tuple[UUID, str]


def _title_label(title_localized: dict[str, str] | None) -> str:
    return primary_text(title_localized) or ""


def _assessment_due_binding_exists() -> exists:
    return exists(
        select(ModuleTriggerBinding.id)
        .join(
            TriggerDefinition,
            ModuleTriggerBinding.trigger_definition_id == TriggerDefinition.id,
        )
        .where(
            ModuleTriggerBinding.module_id == Module.id,
            TriggerDefinition.trigger_kind == "workflow_event",
            TriggerDefinition.predicate_jsonb["spice_event_code"].astext == "assessment_due",
        )
    )


async def _fetch_published_modules(*, missing_only: bool) -> list[ModuleRow]:
    stmt = (
        select(Module.id, Module.title_localized)
        .where(Module.lifecycle_status == "published")
        .order_by(Module.published_at.asc().nullslast(), Module.created_at.asc())
    )
    if missing_only:
        stmt = stmt.where(~_assessment_due_binding_exists())
    async with SessionLocal() as session:
        result = await session.execute(stmt)
        return [(row.id, _title_label(row.title_localized)) for row in result.all()]


async def _fetch_module_by_id(module_id: UUID) -> ModuleRow | None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Module.id, Module.title_localized).where(Module.id == module_id)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return (row.id, _title_label(row.title_localized))


async def _run(*, dry_run: bool, missing_only: bool, module_id: UUID | None) -> int:
    if module_id is not None:
        module = await _fetch_module_by_id(module_id)
        if module is None:
            print(f"Module {module_id} not found.", file=sys.stderr)
            return 1
        modules = [module]
    else:
        modules = await _fetch_published_modules(missing_only=missing_only)

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
        bind_assessment_triggers_task.delay(str(mid))
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
        help="Only published modules that have no assessment-due trigger binding",
    )
    parser.add_argument(
        "--module-id",
        type=UUID,
        default=None,
        help="Bind a single module by ID (skips bulk published-module query)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(dry_run=args.dry_run, missing_only=args.missing_only, module_id=args.module_id))


if __name__ == "__main__":
    raise SystemExit(main())
