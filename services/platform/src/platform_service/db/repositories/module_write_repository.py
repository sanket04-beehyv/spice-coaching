"""Module repository write paths — create, version, publish, retire."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mc_contracts.localized import LocalizedString
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.db.repositories.module_repository_helpers import (
    THUMBNAIL_UNSET,
    ModuleNotFoundError,
    gap_code_for_module,
    slugify,
)
from platform_service.localized import primary_text


class ModuleWriteRepository:
    _session: AsyncSession

    async def create_module(
        self,
        *,
        title: LocalizedString,
        description: LocalizedString | None = None,
        domain: str = "clinical",
        sub_domain: str | None = None,
        module_type: str = "refresher",
        estimated_minutes: int = 10,
        difficulty_level: str = "moderate",
        module_json: dict[str, Any] | None = None,
        creator_id: UUID | None = None,
        behavioural_gap_ids: list[UUID] | None = None,
        primary_gap_id: UUID | None = None,
    ) -> Module:
        proposed_title = primary_text(title) or ""
        slug = slugify(proposed_title)
        candidate_code = slug
        attempt = 0
        while True:
            existing = await self._session.execute(
                select(ModuleFamily).where(ModuleFamily.module_code == candidate_code)
            )
            row = existing.scalar_one_or_none()
            if row is None:
                family = ModuleFamily(module_code=candidate_code, created_by=creator_id)
                self._session.add(family)
                await self._session.flush()
                break
            attempt += 1
            candidate_code = f"{slug}-{attempt}"

        new_module = Module(
            module_family_id=family.id,
            version=1,
            title_localized=title,
            description_localized=description,
            domain=domain,
            sub_domain=sub_domain,
            module_type=module_type,
            estimated_minutes=estimated_minutes,
            difficulty_level=difficulty_level,
            module_json=module_json or {"cards": []},
            lifecycle_status="draft",
            clinically_reviewed=False,
            published_at=None,
        )
        self._session.add(new_module)
        await self._session.flush()

        gap_repo = ModuleGapRepository(self._session)
        if behavioural_gap_ids:
            primary = primary_gap_id
            if primary is None:
                primary = behavioural_gap_ids[0]
            await gap_repo.replace_links(
                new_module.id,
                gap_ids=behavioural_gap_ids,
                primary_gap_id=primary,
            )
        else:
            gap = BehaviouralGap(
                gap_code=gap_code_for_module(new_module.id),
                description=proposed_title,
                domain=domain,
                severity_default="moderate",
                detection_rule_jsonb={},
                status="active",
            )
            self._session.add(gap)
            await self._session.flush()
            await gap_repo.add_primary_link(new_module, behavioural_gap_id=gap.id)

        return new_module

    async def edit_module(
        self,
        module_id: UUID,
        *,
        title: LocalizedString | None = None,
        description: LocalizedString | None = None,
        module_json: dict[str, Any] | None = None,
        visibility_window: Any | None = None,
        thumbnail_storage_path: str | None | object = THUMBNAIL_UNSET,
        editor_id: UUID | None = None,
    ) -> Module:
        current = await self._session.get(Module, module_id)
        if current is None or current.lifecycle_status == "retired":
            raise ModuleNotFoundError(module_id)
        next_version = current.version + 1
        if thumbnail_storage_path is THUMBNAIL_UNSET:
            next_thumbnail = current.thumbnail_storage_path
        else:
            next_thumbnail = thumbnail_storage_path

        next_title_localized = title if title is not None else current.title_localized
        next_description_localized = description if description is not None else current.description_localized

        new_module = Module(
            module_family_id=current.module_family_id,
            version=next_version,
            title_localized=next_title_localized,
            description_localized=next_description_localized,
            domain=current.domain,
            sub_domain=current.sub_domain,
            module_type=current.module_type,
            tenant_id=current.tenant_id,
            primary_gap_id=current.primary_gap_id,
            estimated_minutes=current.estimated_minutes,
            difficulty_level=current.difficulty_level,
            source_document_ids=list(current.source_document_ids or []),
            thumbnail_storage_path=next_thumbnail,
            urgent_publish=current.urgent_publish,
            module_json=copy.deepcopy(module_json)
            if module_json is not None
            else copy.deepcopy(current.module_json),
            visibility_window=visibility_window
            if visibility_window is not None
            else current.visibility_window,
            pass_threshold_override=current.pass_threshold_override,
            clinically_reviewed=False,
            lifecycle_status="draft",
            published_at=None,
            supersedes_module_id=current.id,
        )
        self._session.add(new_module)
        await self._session.flush()

        await ModuleGapRepository(self._session).copy_links(current.id, new_module.id)

        family = await self._session.get(ModuleFamily, current.module_family_id)
        if family is not None:
            family.current_published_module_id = new_module.id
        await self._session.flush()
        return new_module

    async def set_clinically_reviewed(
        self,
        module_id: UUID,
        *,
        flag: bool,
        reviewer_id: UUID | None = None,
    ) -> Module:
        module = await self._session.get(Module, module_id)
        if module is None or module.lifecycle_status == "retired":
            raise ModuleNotFoundError(module_id)
        module.clinically_reviewed = flag
        module.clinically_reviewed_at = datetime.now(UTC) if flag else None
        module.clinically_reviewed_by = reviewer_id if flag else None

        if flag:
            module.lifecycle_status = "published"
            module.published_at = datetime.now(UTC)

            family = await self._session.get(ModuleFamily, module.module_family_id)
            if family is not None:
                family.current_published_module_id = module.id

            stmt = select(Module).where(
                Module.module_family_id == module.module_family_id,
                Module.id != module.id,
                Module.lifecycle_status != "retired",
            )
            older_modules = (await self._session.execute(stmt)).scalars().all()
            for old_mod in older_modules:
                old_mod.lifecycle_status = "retired"
                old_mod.deprecated_at = datetime.now(UTC)

        await self._session.flush()
        return module

    async def set_visibility_window(
        self,
        module_id: UUID,
        *,
        window: Any | None,
    ) -> Module:
        module = await self._session.get(Module, module_id)
        if module is None or module.lifecycle_status == "retired":
            raise ModuleNotFoundError(module_id)
        module.visibility_window = window
        await self._session.flush()
        return module

    async def retire_module(self, module_id: UUID) -> Module:
        module = await self._session.get(Module, module_id)
        if module is None:
            raise ModuleNotFoundError(module_id)
        module.lifecycle_status = "retired"
        module.deprecated_at = datetime.now(UTC)
        family = await self._session.get(ModuleFamily, module.module_family_id)
        if family is not None and family.current_published_module_id == module.id:
            stmt = (
                select(Module)
                .where(
                    Module.module_family_id == module.module_family_id,
                    Module.id != module.id,
                    Module.lifecycle_status == "published",
                )
                .order_by(Module.version.desc())
                .limit(1)
            )
            other = (await self._session.execute(stmt)).scalar_one_or_none()
            family.current_published_module_id = other.id if other else None
        await self._session.flush()
        return module

    async def patch_card_search_metadata(
        self,
        module_id: UUID,
        card_index: int,
        metadata: dict[str, Any],
    ) -> Module:
        """Atomically write search_metadata onto one card inside module_json."""
        stmt = select(Module).where(Module.id == module_id).with_for_update()
        result = await self._session.execute(stmt)
        module = result.scalar_one_or_none()
        if module is None:
            raise ModuleNotFoundError(module_id)

        module_json = copy.deepcopy(module.module_json or {"cards": []})
        cards = module_json.get("cards", [])
        if not isinstance(cards, list):
            raise ValueError(f"module {module_id} module_json.cards is not a list")
        if card_index < 0 or card_index >= len(cards):
            raise ValueError(f"card_index {card_index} out of range for module {module_id}")
        card = cards[card_index]
        if not isinstance(card, dict):
            raise ValueError(f"card at index {card_index} is not a dict")

        cards[card_index] = {**card, "search_metadata": metadata}
        module_json["cards"] = cards
        module.module_json = module_json
        await self._session.flush()
        return module

    async def patch_cards_search_metadata(
        self,
        module_id: UUID,
        metadata_by_index: dict[int, dict[str, Any]],
    ) -> Module:
        """Atomically write search_metadata onto multiple cards inside module_json."""
        if not metadata_by_index:
            stmt = select(Module).where(Module.id == module_id)
            result = await self._session.execute(stmt)
            module = result.scalar_one_or_none()
            if module is None:
                raise ModuleNotFoundError(module_id)
            return module

        stmt = select(Module).where(Module.id == module_id).with_for_update()
        result = await self._session.execute(stmt)
        module = result.scalar_one_or_none()
        if module is None:
            raise ModuleNotFoundError(module_id)

        module_json = copy.deepcopy(module.module_json or {"cards": []})
        cards = module_json.get("cards", [])
        if not isinstance(cards, list):
            raise ValueError(f"module {module_id} module_json.cards is not a list")

        for card_index, metadata in metadata_by_index.items():
            if card_index < 0 or card_index >= len(cards):
                raise ValueError(f"card_index {card_index} out of range for module {module_id}")
            card = cards[card_index]
            if not isinstance(card, dict):
                raise ValueError(f"card at index {card_index} is not a dict")
            cards[card_index] = {**card, "search_metadata": metadata}

        module_json["cards"] = cards
        module.module_json = module_json
        await self._session.flush()
        return module
