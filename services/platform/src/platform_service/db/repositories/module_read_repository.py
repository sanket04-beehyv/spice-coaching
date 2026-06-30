"""Module repository read paths — listings, search, and sync queries."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.module import Module
from platform_service.db.models.module_behavioural_gap import ModuleBehaviouralGap
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.tenant_scope import tenant_scope_filter
from platform_service.localized import deployment_locales


def _escape_ilike_pattern(value: str) -> str:
    """Escape SQL ``LIKE``/``ILIKE`` wildcards in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ModuleReadRepository:
    _session: AsyncSession

    async def list_modules(
        self,
        *,
        status: str | None = None,
        clinically_reviewed: bool | None = None,
        has_visibility_window: bool | None = None,
        has_quality_flags: bool | None = None,
        domain: str | None = None,
        full_text_query: str | None = None,
        latest_version_only: bool = False,
        tenant_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Module]:
        stmt = select(Module)
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        if status is not None:
            stmt = stmt.where(Module.lifecycle_status == status)
        else:
            stmt = stmt.where(Module.lifecycle_status != "retired")
        if clinically_reviewed is not None:
            stmt = stmt.where(Module.clinically_reviewed == clinically_reviewed)
        if has_visibility_window is True:
            stmt = stmt.where(Module.visibility_window.isnot(None))
        elif has_visibility_window is False:
            stmt = stmt.where(Module.visibility_window.is_(None))
        if has_quality_flags is True:
            stmt = stmt.where(
                Module.quality_flags_jsonb.isnot(None),
                Module.quality_flags_jsonb != cast({}, JSONB),
            )
        elif has_quality_flags is False:
            stmt = stmt.where(
                or_(
                    Module.quality_flags_jsonb.is_(None),
                    Module.quality_flags_jsonb == cast({}, JSONB),
                )
            )
        if domain:
            stmt = stmt.where(Module.domain == domain)
        if full_text_query:
            escaped = _escape_ilike_pattern(full_text_query)
            pattern = f"%{escaped}%"
            primary = deployment_locales()
            search_exprs = [
                Module.title_localized[primary].astext.ilike(pattern, escape="\\"),
                Module.description_localized[primary].astext.ilike(pattern, escape="\\"),
            ]
            stmt = stmt.where(or_(*search_exprs))
        if latest_version_only:
            rank_sq = select(
                Module.id,
                func.row_number()
                .over(
                    partition_by=Module.module_family_id,
                    order_by=Module.version.desc(),
                )
                .label("rn"),
            ).subquery()
            stmt = stmt.join(rank_sq, Module.id == rank_sq.c.id).where(rank_sq.c.rn == 1)
        stmt = (
            stmt.order_by(Module.published_at.desc().nullslast(), Module.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_module(self, module_id: UUID) -> Module | None:
        return await self._session.get(Module, module_id)

    async def list_quiz_questions(self, module_id: UUID) -> list[ModuleQuizQuestion]:
        result = await self._session.execute(
            select(ModuleQuizQuestion)
            .where(ModuleQuizQuestion.module_id == module_id)
            .order_by(ModuleQuizQuestion.question_order.asc().nullslast())
        )
        return list(result.scalars().all())

    async def list_families_created_since(
        self,
        since: datetime,
        *,
        tenant_id: UUID | None = None,
    ) -> list[ModuleFamily]:
        stmt = (
            select(ModuleFamily)
            .where(ModuleFamily.created_at > since)
            .order_by(ModuleFamily.created_at.asc(), ModuleFamily.id.asc())
        )
        if tenant_id is not None:
            stmt = (
                stmt.join(Module, Module.module_family_id == ModuleFamily.id)
                .where(tenant_scope_filter(Module.tenant_id, tenant_id))
                .distinct()
            )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_published_modules_updated_since(
        self,
        since: datetime,
        *,
        tenant_id: UUID | None = None,
    ) -> list[Module]:
        stmt = (
            select(Module)
            .where(Module.lifecycle_status == "published", Module.updated_at > since)
            .order_by(Module.updated_at.asc(), Module.id.asc())
        )
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_modules_by_ids(
        self,
        module_ids: list[UUID],
        *,
        tenant_id: UUID | None = None,
    ) -> list[Module]:
        if not module_ids:
            return []
        stmt = select(Module).where(Module.id.in_(module_ids))
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        return list((await self._session.execute(stmt)).scalars().all())

    async def filter_source_document_ids_for_tenant(
        self,
        source_document_ids: list[UUID],
        tenant_id: UUID,
    ) -> set[UUID]:
        if not source_document_ids:
            return set()
        requested = set(source_document_ids)
        stmt = select(Module.source_document_ids).where(
            Module.lifecycle_status == "published",
            tenant_scope_filter(Module.tenant_id, tenant_id),
            Module.source_document_ids.isnot(None),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        allowed: set[UUID] = set()
        for doc_ids in rows:
            if doc_ids:
                allowed.update(doc_id for doc_id in doc_ids if doc_id in requested)
        return allowed

    async def list_quiz_questions_for_module_ids(self, module_ids: list[UUID]) -> list[ModuleQuizQuestion]:
        if not module_ids:
            return []
        stmt = (
            select(ModuleQuizQuestion)
            .where(ModuleQuizQuestion.module_id.in_(module_ids))
            .order_by(
                ModuleQuizQuestion.module_id.asc(),
                ModuleQuizQuestion.question_order.asc().nullslast(),
                ModuleQuizQuestion.id.asc(),
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def search_by_embedding(
        self,
        *,
        query_vector: list[float],
        limit: int = 10,
        tenant_id: UUID | None = None,
    ) -> list[tuple[Module, float]]:
        distance = Module.embedding.cosine_distance(list(query_vector)).label("distance")
        stmt = (
            select(Module, distance)
            .where(Module.embedding.is_not(None), Module.lifecycle_status == "published")
            .order_by(distance.asc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        rows = (await self._session.execute(stmt)).all()
        return [(mod, float(dist)) for mod, dist in rows]

    async def list_published_modules_for_gap_ids(
        self,
        *,
        gap_ids: list[UUID],
        tenant_id: UUID,
    ) -> list[Module]:
        if not gap_ids:
            return []
        tenant_filter = or_(Module.tenant_id.is_(None), Module.tenant_id == tenant_id)
        link_exists = (
            select(ModuleBehaviouralGap.id)
            .where(
                ModuleBehaviouralGap.module_id == Module.id,
                ModuleBehaviouralGap.behavioural_gap_id.in_(gap_ids),
            )
            .exists()
        )
        stmt = select(Module).where(
            Module.lifecycle_status == "published",
            link_exists,
            tenant_filter,
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_published_modules_for_primary_gap_ids(
        self,
        *,
        gap_ids: list[UUID],
        tenant_id: UUID,
    ) -> list[Module]:
        return await self.list_published_modules_for_gap_ids(
            gap_ids=gap_ids,
            tenant_id=tenant_id,
        )

    async def list_active_modules_for_merge(self) -> list[Module]:
        rank_sq = (
            select(
                Module.id,
                func.row_number()
                .over(
                    partition_by=Module.module_family_id,
                    order_by=Module.version.desc(),
                )
                .label("rn"),
            )
            .where(Module.lifecycle_status != "retired")
            .subquery()
        )
        stmt = select(Module).join(rank_sq, Module.id == rank_sq.c.id).where(rank_sq.c.rn == 1)
        result = await self._session.execute(stmt)
        modules = list(result.scalars().all())
        out: list[Module] = []
        for mod in modules:
            cards = (mod.module_json or {}).get("cards", [])
            if isinstance(cards, list) and len(cards) > 0:
                out.append(mod)
        return out

    async def family_has_draft_other_than(
        self,
        module_family_id: UUID,
        *,
        exclude_module_id: UUID | None = None,
    ) -> bool:
        stmt = select(Module.id).where(
            Module.module_family_id == module_family_id,
            Module.lifecycle_status == "draft",
        )
        if exclude_module_id is not None:
            stmt = stmt.where(Module.id != exclude_module_id)
        stmt = stmt.limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def list_latest_published_by_family_ids(
        self,
        family_ids: list[UUID],
        *,
        tenant_id: UUID | None = None,
    ) -> dict[UUID, Module]:
        """Return the latest published module row per family id."""
        if not family_ids:
            return {}
        filters = [
            Module.lifecycle_status == "published",
            Module.module_family_id.in_(family_ids),
        ]
        if tenant_id is not None:
            filters.append(tenant_scope_filter(Module.tenant_id, tenant_id))
        rank_sq = (
            select(
                Module.id,
                func.row_number()
                .over(
                    partition_by=Module.module_family_id,
                    order_by=Module.created_at.desc(),
                )
                .label("rn"),
            )
            .where(*filters)
            .subquery()
        )
        stmt = select(Module).join(rank_sq, Module.id == rank_sq.c.id).where(rank_sq.c.rn == 1)
        modules = list((await self._session.execute(stmt)).scalars().all())
        return {mod.module_family_id: mod for mod in modules}

    async def list_recent_published_one_per_family(
        self,
        *,
        tenant_id: UUID,
        limit: int = 5,
    ) -> list[Module]:
        tenant_filter = or_(Module.tenant_id.is_(None), Module.tenant_id == tenant_id)
        rank_sq = (
            select(
                Module.id,
                func.row_number()
                .over(
                    partition_by=Module.module_family_id,
                    order_by=Module.created_at.desc(),
                )
                .label("rn"),
            )
            .where(Module.lifecycle_status == "published", tenant_filter)
            .subquery()
        )
        stmt = (
            select(Module)
            .join(rank_sq, Module.id == rank_sq.c.id)
            .where(rank_sq.c.rn == 1)
            .order_by(Module.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_recent_published_modules_by_published_at(
        self,
        *,
        tenant_id: UUID | None = None,
        limit: int = 5,
    ) -> list[Module]:
        """Return newest published modules by published_at (desc), optionally tenant scoped."""
        stmt = select(Module).where(Module.lifecycle_status == "published")
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        stmt = stmt.order_by(Module.published_at.desc().nullslast(), Module.id.asc()).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_modules(
        self,
        *,
        status: str | None = None,
        clinically_reviewed: bool | None = None,
    ) -> int:
        stmt = select(func.count(Module.id))
        if status is not None:
            stmt = stmt.where(Module.lifecycle_status == status)
        else:
            stmt = stmt.where(Module.lifecycle_status != "retired")
        if clinically_reviewed is not None:
            stmt = stmt.where(Module.clinically_reviewed == clinically_reviewed)
        return int((await self._session.execute(stmt)).scalar_one())
