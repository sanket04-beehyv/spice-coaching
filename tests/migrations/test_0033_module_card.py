"""Schema verification for migration 0033 (module_card normalization)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import requires_db

pytestmark = [requires_db]


async def _table_exists(session: AsyncSession, table: str) -> bool:
    row = (
        await session.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=:t"),
            {"t": table},
        )
    ).first()
    return row is not None


@pytest.mark.asyncio
async def test_module_card_table_exists(db_session: AsyncSession) -> None:
    assert await _table_exists(db_session, "module_card")


@pytest.mark.asyncio
async def test_module_json_has_no_cards_key_on_published_modules(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(text("SELECT count(*) FROM module WHERE module_json ? 'cards'"))
    ).scalar_one()
    assert int(rows) == 0
