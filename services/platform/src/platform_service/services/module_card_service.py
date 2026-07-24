"""Persist module card versions when pipeline or admin writes a module."""

from __future__ import annotations

import copy
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module_card import ModuleCard
from platform_service.services.card_normalisation import card_dict_to_row_fields


def extract_cards_from_module_json(module_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not module_json:
        return []
    cards = module_json.get("cards")
    if not isinstance(cards, list):
        return []
    return [dict(card) for card in cards if isinstance(card, dict)]


def module_json_shell(module_json: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return module-level JSON without inline cards or quiz payloads."""
    if module_json is None:
        return None
    shell = copy.deepcopy(module_json)
    shell.pop("cards", None)
    shell.pop("quiz", None)
    if not shell:
        return None
    return shell


class ModuleCardService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_cards(
        self,
        module_id: UUID,
        cards: list[dict[str, Any]],
    ) -> None:
        """Write versioned card rows for a newly created module version."""
        for idx, raw_card in enumerate(cards, start=1):
            card = dict(raw_card)
            row_fields = card_dict_to_row_fields(card)
            title_localized = row_fields.get("title_localized")
            if not title_localized:
                continue

            card_family_id = uuid.uuid4()
            card_version = 1
            card_order = int(card.get("card_order") or idx)

            family_raw = card.get("card_family_id")
            if family_raw:
                try:
                    parsed_family = UUID(str(family_raw))
                    stmt = (
                        select(ModuleCard)
                        .where(ModuleCard.card_family_id == parsed_family)
                        .order_by(ModuleCard.card_version.desc())
                        .limit(1)
                    )
                    existing = (await self._session.execute(stmt)).scalar_one_or_none()
                    if existing is not None:
                        card_family_id = existing.card_family_id
                        card_version = int(existing.card_version) + 1
                except ValueError:
                    pass

            row = ModuleCard(
                module_id=module_id,
                card_order=card_order,
                card_family_id=card_family_id,
                card_version=card_version,
                title_localized=title_localized,
                **{key: value for key, value in row_fields.items() if key != "title_localized"},
            )
            self._session.add(row)

    async def replace_cards(
        self,
        module_id: UUID,
        cards: list[dict[str, Any]],
    ) -> None:
        """Delete existing rows for ``module_id`` and write a fresh card set."""
        result = await self._session.execute(select(ModuleCard).where(ModuleCard.module_id == module_id))
        for existing in result.scalars().all():
            await self._session.delete(existing)
        await self._session.flush()
        await self.append_cards(module_id, cards)
