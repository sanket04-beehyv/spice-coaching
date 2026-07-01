#!/usr/bin/env -S uv run python
"""Strip card-number citations from persisted quiz explanation_localized maps.

Older quiz-generation prompts instructed the LLM to cite card indices inside
explanation prose. Safe to re-run (idempotent on already-clean rows).

Usage:
    uv run python bin/backfill_quiz_explanation_card_citations.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from platform_service.config import get_settings
from platform_service.services.quiz_explanation_sanitizer import (
    sanitize_explanation_localized_value,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)


async def _backfill(session: AsyncSession, *, dry_run: bool) -> int:
    result = await session.execute(
        text(
            "SELECT id, explanation_localized FROM module_quiz_question "
            "WHERE explanation_localized IS NOT NULL"
        )
    )
    rows = result.fetchall()
    updated = 0
    for question_id, explanation_localized in rows:
        sanitized = sanitize_explanation_localized_value(explanation_localized)
        if sanitized == explanation_localized:
            continue
        updated += 1
        if dry_run:
            logger.info("would update quiz question %s", question_id)
            continue
        await session.execute(
            text(
                "UPDATE module_quiz_question "
                "SET explanation_localized = CAST(:explanation_localized AS jsonb) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {
                "id": str(question_id),
                "explanation_localized": json.dumps(sanitized) if sanitized is not None else None,
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
    logger.info("updated %d quiz question(s)", count)


if __name__ == "__main__":
    asyncio.run(main())
