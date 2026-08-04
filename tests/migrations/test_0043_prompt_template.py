"""Schema verification for prompt_template migrations."""

from __future__ import annotations

import json
from pathlib import Path

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
async def test_prompt_template_table_exists(db_session: AsyncSession) -> None:
    assert await _table_exists(db_session, "prompt_template")


@pytest.mark.asyncio
async def test_seed_file_lists_all_catalog_templates() -> None:
    seed_path = Path(__file__).resolve().parents[2] / "seed" / "prompt_templates.json"
    rows = json.loads(seed_path.read_text(encoding="utf-8"))
    template_ids = {row["template_id"] for row in rows}
    assert "module-identifier" in template_ids
    assert "coaching-rag" in template_ids
    assert len(template_ids) >= 14
