"""Tests for ModuleCardService append/versioning."""

from __future__ import annotations

from uuid import uuid4

import pytest
from platform_service.db.models.module import Module
from platform_service.db.models.module_card import ModuleCard
from platform_service.db.models.module_family import ModuleFamily
from platform_service.services.module_card_service import ModuleCardService
from sqlalchemy import select

from tests.conftest import requires_db

pytestmark = [requires_db]


@pytest.mark.asyncio
async def test_append_cards_mints_family_id(db_session) -> None:
    family = ModuleFamily(module_code=f"test-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()

    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": "টেস্ট"},
        domain="clinical",
        module_type="refresher",
        lifecycle_status="draft",
    )
    db_session.add(module)
    await db_session.flush()

    await ModuleCardService(db_session).append_cards(
        module.id,
        [{"title": {"bn": "কার্ড এক"}, "body": {"bn": "বডি"}}],
    )
    await db_session.flush()

    rows = (
        (await db_session.execute(select(ModuleCard).where(ModuleCard.module_id == module.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].card_order == 1
    assert rows[0].card_version == 1
    assert rows[0].title_localized == {"bn": "কার্ড এক"}


@pytest.mark.asyncio
async def test_append_cards_reuses_family_on_edit(db_session) -> None:
    family = ModuleFamily(module_code=f"test-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()

    module = Module(
        module_family_id=family.id,
        version=1,
        title_localized={"bn": "টেস্ট"},
        domain="clinical",
        module_type="refresher",
        lifecycle_status="draft",
    )
    db_session.add(module)
    await db_session.flush()

    family_id = uuid4()
    await ModuleCardService(db_session).append_cards(
        module.id,
        [
            {
                "card_family_id": str(family_id),
                "title": {"bn": "কার্ড"},
                "body": {"bn": "বডি"},
            }
        ],
    )
    await db_session.flush()

    module2 = Module(
        module_family_id=family.id,
        version=2,
        title_localized={"bn": "টেস্ট"},
        domain="clinical",
        module_type="refresher",
        lifecycle_status="draft",
    )
    db_session.add(module2)
    await db_session.flush()

    await ModuleCardService(db_session).append_cards(
        module2.id,
        [
            {
                "card_family_id": str(family_id),
                "title": {"bn": "কার্ড সম্পাদিত"},
                "body": {"bn": "নতুন বডি"},
            }
        ],
    )
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(ModuleCard)
                .where(ModuleCard.card_family_id == family_id)
                .order_by(ModuleCard.card_version.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert rows[0].card_version == 1
    assert rows[1].card_version == 2
    assert rows[1].module_id == module2.id


@pytest.mark.asyncio
async def test_append_cards_bumps_from_highest_existing_version(db_session) -> None:
    family = ModuleFamily(module_code=f"test-{uuid4().hex[:8]}")
    db_session.add(family)
    await db_session.flush()

    modules = []
    for version in (1, 2, 3):
        module = Module(
            module_family_id=family.id,
            version=version,
            title_localized={"bn": "টেস্ট"},
            domain="clinical",
            module_type="refresher",
            lifecycle_status="draft",
        )
        db_session.add(module)
        modules.append(module)
    await db_session.flush()

    family_id = uuid4()
    for module in modules:
        await ModuleCardService(db_session).append_cards(
            module.id,
            [
                {
                    "card_family_id": str(family_id),
                    "title": {"bn": "কার্ড"},
                    "body": {"bn": "বডি"},
                }
            ],
        )
        await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(ModuleCard)
                .where(ModuleCard.card_family_id == family_id)
                .order_by(ModuleCard.card_version.asc())
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 3
    assert [row.card_version for row in rows] == [1, 2, 3]
    assert rows[2].module_id == modules[2].id
