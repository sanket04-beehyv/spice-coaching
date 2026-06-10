"""Repository for module ↔ behavioural_gap junction rows."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.models.module_behavioural_gap import ModuleBehaviouralGap


class ModuleGapLinkError(Exception):
    """Invalid gap link arguments."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ModuleGapRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_gap_ids(self, module_id: UUID) -> list[UUID]:
        links = await self.get_links(module_id)
        return [link.behavioural_gap_id for link in links]

    async def get_links(self, module_id: UUID) -> list[ModuleBehaviouralGap]:
        result = await self._session.execute(
            select(ModuleBehaviouralGap)
            .where(ModuleBehaviouralGap.module_id == module_id)
            .order_by(ModuleBehaviouralGap.is_primary.desc())
        )
        return list(result.scalars().all())

    async def get_gap_ids_by_module_ids(
        self,
        module_ids: list[UUID],
    ) -> dict[UUID, list[UUID]]:
        if not module_ids:
            return {}
        result = await self._session.execute(
            select(ModuleBehaviouralGap).where(ModuleBehaviouralGap.module_id.in_(module_ids))
        )
        out: dict[UUID, list[UUID]] = {mid: [] for mid in module_ids}
        for link in result.scalars().all():
            out[link.module_id].append(link.behavioural_gap_id)
        return out

    async def add_primary_link(self, module: Module, *, behavioural_gap_id: UUID) -> None:
        """Attach a single primary gap and sync ``module.primary_gap_id``."""
        await self.replace_links(
            module.id,
            gap_ids=[behavioural_gap_id],
            primary_gap_id=behavioural_gap_id,
        )

    async def replace_secondary_links(
        self,
        module_id: UUID,
        *,
        secondary_gap_ids: list[UUID],
        primary_gap_id: UUID,
    ) -> None:
        """Replace non-primary gap links; keep ``primary_gap_id`` unchanged."""
        unique_secondary = [gid for gid in dict.fromkeys(secondary_gap_ids) if gid != primary_gap_id]
        await self.replace_links(
            module_id,
            gap_ids=[primary_gap_id, *unique_secondary],
            primary_gap_id=primary_gap_id,
        )

    async def replace_links(
        self,
        module_id: UUID,
        *,
        gap_ids: list[UUID],
        primary_gap_id: UUID | None,
    ) -> None:
        unique_gap_ids = list(dict.fromkeys(gap_ids))
        if primary_gap_id is not None and primary_gap_id not in unique_gap_ids:
            raise ModuleGapLinkError("primary_gap_id must be included in behavioural_gap_ids")
        if not unique_gap_ids and primary_gap_id is not None:
            raise ModuleGapLinkError("primary_gap_id requires at least one behavioural_gap_id")

        module = await self._session.get(Module, module_id)
        if module is None:
            raise ModuleGapLinkError(f"module {module_id} not found")

        existing = await self.get_links(module_id)
        existing_by_gap = {link.behavioural_gap_id: link for link in existing}
        desired = set(unique_gap_ids)

        for gap_id in desired:
            is_primary = gap_id == primary_gap_id
            row = existing_by_gap.get(gap_id)
            if row is None:
                self._session.add(
                    ModuleBehaviouralGap(
                        module_id=module_id,
                        behavioural_gap_id=gap_id,
                        is_primary=is_primary,
                    )
                )
            elif row.is_primary != is_primary:
                row.is_primary = is_primary

        for link in existing:
            if link.behavioural_gap_id not in desired:
                await self._session.delete(link)

        module.primary_gap_id = primary_gap_id
        await self._session.flush()

    async def copy_links(self, from_module_id: UUID, to_module_id: UUID) -> None:
        links = await self.get_links(from_module_id)
        if not links:
            to_module = await self._session.get(Module, to_module_id)
            if to_module is not None:
                to_module.primary_gap_id = None
            await self._session.flush()
            return

        primary = next((link.behavioural_gap_id for link in links if link.is_primary), None)
        if primary is None and links:
            primary = links[0].behavioural_gap_id
        await self.replace_links(
            to_module_id,
            gap_ids=[link.behavioural_gap_id for link in links],
            primary_gap_id=primary,
        )
