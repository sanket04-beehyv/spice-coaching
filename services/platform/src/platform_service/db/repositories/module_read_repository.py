"""Module repository read paths — listings, search, and sync queries."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, Text, cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from platform_service.db.models.module import Module
from platform_service.db.models.module_behavioural_gap import ModuleBehaviouralGap
from platform_service.db.models.module_card import ModuleCard
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.module_availability import (
    DEFAULT_EXCLUDED_LIFECYCLE_STATUSES,
    is_training_module_family,
)
from platform_service.db.tenant_scope import tenant_scope_filter
from platform_service.localized import deployment_locales, primary_text
from platform_service.vectorstore import MODULES_COLLECTION, get_vector_store


def _escape_ilike_pattern(value: str) -> str:
    """Escape SQL ``LIKE``/``ILIKE`` wildcards in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


MODULE_SORT_KEYS = frozenset(
    {
        "created_at",
        "published_at",
        "activated_at",
        "last_deactivated_at",
        "title",
        "domain",
        "lifecycle_status",
    }
)
MODULE_SORT_DIRS = frozenset({"asc", "desc"})
DEFAULT_MODULE_SORT_BY = "published_at"
DEFAULT_MODULE_SORT_DIR = "desc"


def _nullable_datetime_order(column, *, descending: bool):
    if descending:
        return column.desc().nullslast()
    return column.asc().nullsfirst()


def _module_order_clauses(sort_by: str, sort_dir: str) -> list[Any]:
    descending = sort_dir == "desc"
    order_fn = (lambda col: col.desc()) if descending else (lambda col: col.asc())

    if sort_by == "created_at":
        primary = order_fn(Module.created_at)
    elif sort_by == "published_at":
        primary = _nullable_datetime_order(Module.published_at, descending=descending)
    elif sort_by == "activated_at":
        primary = _nullable_datetime_order(ModuleReadRepository._activated_at_expr(), descending=descending)
    elif sort_by == "last_deactivated_at":
        primary = _nullable_datetime_order(Module.last_deactivated_at, descending=descending)
    elif sort_by == "title":
        primary_locale = deployment_locales()
        primary = order_fn(Module.title_localized[primary_locale].astext)
    elif sort_by == "domain":
        primary = order_fn(Module.domain)
    elif sort_by == "lifecycle_status":
        primary = order_fn(Module.lifecycle_status)
    else:
        raise ValueError(f"unsupported sort_by: {sort_by}")

    return [primary, order_fn(Module.id)]


class ModuleReadRepository:
    _session: AsyncSession

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _activated_at_expr():
        """Matches dashboard ``getModuleActivatedAt`` coalesce order."""
        return func.coalesce(
            Module.last_reactivated_at,
            Module.first_activated_at,
            Module.published_at,
        )

    @staticmethod
    def _apply_date_range(
        stmt: Select[tuple[Module]],
        column,
        date_from: datetime | None,
        date_to: datetime | None,
    ) -> Select[tuple[Module]]:
        if date_from is not None:
            stmt = stmt.where(column >= date_from)
        if date_to is not None:
            stmt = stmt.where(column <= date_to)
        return stmt

    def _modules_list_filtered_stmt(
        self,
        *,
        status: str | None = None,
        clinically_reviewed: bool | None = None,
        has_visibility_window: bool | None = None,
        has_quality_flags: bool | None = None,
        domain: str | None = None,
        source_document_id: UUID | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        published_from: datetime | None = None,
        published_to: datetime | None = None,
        activated_from: datetime | None = None,
        activated_to: datetime | None = None,
        deactivated_from: datetime | None = None,
        deactivated_to: datetime | None = None,
        full_text_query: str | None = None,
        latest_version_only: bool = False,
        tenant_id: UUID | None = None,
    ) -> Select[tuple[Module]]:
        """Shared filter tree for ``list_modules`` / ``count_modules`` (no order/limit)."""
        stmt = select(Module)
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        if status is not None:
            stmt = stmt.where(Module.lifecycle_status == status)
        else:
            stmt = stmt.where(Module.lifecycle_status.notin_(sorted(DEFAULT_EXCLUDED_LIFECYCLE_STATUSES)))
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
        if source_document_id is not None:
            stmt = stmt.where(Module.source_document_ids.contains([source_document_id]))
        stmt = self._apply_date_range(stmt, Module.created_at, created_from, created_to)
        stmt = self._apply_date_range(stmt, Module.published_at, published_from, published_to)
        stmt = self._apply_date_range(stmt, self._activated_at_expr(), activated_from, activated_to)
        stmt = self._apply_date_range(stmt, Module.last_deactivated_at, deactivated_from, deactivated_to)
        if full_text_query:
            escaped = _escape_ilike_pattern(full_text_query)
            pattern = f"%{escaped}%"
            search_exprs = [
                cast(Module.title_localized, Text).ilike(pattern, escape="\\"),
                cast(Module.description_localized, Text).ilike(pattern, escape="\\"),
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
        return stmt

    async def list_modules(
        self,
        *,
        status: str | None = None,
        clinically_reviewed: bool | None = None,
        has_visibility_window: bool | None = None,
        has_quality_flags: bool | None = None,
        domain: str | None = None,
        source_document_id: UUID | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        published_from: datetime | None = None,
        published_to: datetime | None = None,
        activated_from: datetime | None = None,
        activated_to: datetime | None = None,
        deactivated_from: datetime | None = None,
        deactivated_to: datetime | None = None,
        full_text_query: str | None = None,
        latest_version_only: bool = False,
        tenant_id: UUID | None = None,
        sort_by: str = DEFAULT_MODULE_SORT_BY,
        sort_dir: str = DEFAULT_MODULE_SORT_DIR,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Module]:
        stmt = self._modules_list_filtered_stmt(
            status=status,
            clinically_reviewed=clinically_reviewed,
            has_visibility_window=has_visibility_window,
            has_quality_flags=has_quality_flags,
            domain=domain,
            source_document_id=source_document_id,
            created_from=created_from,
            created_to=created_to,
            published_from=published_from,
            published_to=published_to,
            activated_from=activated_from,
            activated_to=activated_to,
            deactivated_from=deactivated_from,
            deactivated_to=deactivated_to,
            full_text_query=full_text_query,
            latest_version_only=latest_version_only,
            tenant_id=tenant_id,
        )
        stmt = stmt.order_by(*_module_order_clauses(sort_by, sort_dir)).limit(limit).offset(offset)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_modules(
        self,
        *,
        status: str | None = None,
        clinically_reviewed: bool | None = None,
        has_visibility_window: bool | None = None,
        has_quality_flags: bool | None = None,
        domain: str | None = None,
        source_document_id: UUID | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        published_from: datetime | None = None,
        published_to: datetime | None = None,
        activated_from: datetime | None = None,
        activated_to: datetime | None = None,
        deactivated_from: datetime | None = None,
        deactivated_to: datetime | None = None,
        full_text_query: str | None = None,
        latest_version_only: bool = False,
        tenant_id: UUID | None = None,
    ) -> int:
        """Count rows matching the same filters as ``list_modules`` (ignores limit/offset)."""
        base = self._modules_list_filtered_stmt(
            status=status,
            clinically_reviewed=clinically_reviewed,
            has_visibility_window=has_visibility_window,
            has_quality_flags=has_quality_flags,
            domain=domain,
            source_document_id=source_document_id,
            created_from=created_from,
            created_to=created_to,
            published_from=published_from,
            published_to=published_to,
            activated_from=activated_from,
            activated_to=activated_to,
            deactivated_from=deactivated_from,
            deactivated_to=deactivated_to,
            full_text_query=full_text_query,
            latest_version_only=latest_version_only,
            tenant_id=tenant_id,
        )
        # maintain_column_froms keeps the latest_version_only JOIN when we
        # project down to Module.id for counting.
        id_subq = base.with_only_columns(Module.id, maintain_column_froms=True).order_by(None).subquery()
        count_stmt = select(func.count()).select_from(id_subq)
        result = await self._session.execute(count_stmt)
        return int(result.scalar_one())

    async def list_module_domains(
        self,
        *,
        status: str | None = None,
        latest_version_only: bool = True,
        tenant_id: UUID | None = None,
    ) -> list[str]:
        """Distinct module.domain values for admin filter dropdowns (tab-scoped)."""
        stmt = select(Module.domain)
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        if status is not None:
            stmt = stmt.where(Module.lifecycle_status == status)
        else:
            stmt = stmt.where(Module.lifecycle_status.notin_(sorted(DEFAULT_EXCLUDED_LIFECYCLE_STATUSES)))
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
        stmt = stmt.distinct().order_by(Module.domain.asc())
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_module(self, module_id: UUID) -> Module | None:
        return await self._session.get(Module, module_id)

    async def get_published_module_for_family(
        self,
        module_family_id: UUID,
        *,
        tenant_id: UUID | None = None,
    ) -> Module | None:
        """Return the current published module for a family, or None.

        Returns None for chatbot-FAQ-only modules (not available for CHW training).
        """
        family = await self._session.get(ModuleFamily, module_family_id)
        if family is None or family.current_published_module_id is None:
            return None
        module = await self.get_module(family.current_published_module_id)
        if module is None or module.lifecycle_status != "published" or module.chatbot_faqs_only:
            return None
        if tenant_id is not None and module.tenant_id is not None and module.tenant_id != tenant_id:
            return None
        return module

    async def map_published_titles_by_family_ids(
        self,
        module_family_ids: list[UUID],
    ) -> dict[UUID, str]:
        """Return display titles for families that have a current published module."""
        if not module_family_ids:
            return {}
        stmt = (
            select(ModuleFamily.id, Module.title_localized)
            .join(Module, Module.id == ModuleFamily.current_published_module_id)
            .where(
                ModuleFamily.id.in_(module_family_ids),
                Module.lifecycle_status == "published",
                is_training_module_family(),
            )
        )
        rows = (await self._session.execute(stmt)).all()
        out: dict[UUID, str] = {}
        for family_id, title_localized in rows:
            title = primary_text(title_localized)
            if title:
                out[family_id] = title
        return out

    async def list_quiz_questions(self, module_id: UUID) -> list[ModuleQuizQuestion]:
        result = await self._session.execute(
            select(ModuleQuizQuestion)
            .where(ModuleQuizQuestion.module_id == module_id)
            .order_by(ModuleQuizQuestion.question_order.asc().nullslast())
        )
        return list(result.scalars().all())

    async def list_cards(self, module_id: UUID) -> list[ModuleCard]:
        result = await self._session.execute(
            select(ModuleCard)
            .where(ModuleCard.module_id == module_id)
            .order_by(ModuleCard.card_order.asc(), ModuleCard.id.asc())
        )
        return list(result.scalars().all())

    async def list_cards_for_module_ids(self, module_ids: list[UUID]) -> list[ModuleCard]:
        if not module_ids:
            return []
        stmt = (
            select(ModuleCard)
            .where(ModuleCard.module_id.in_(module_ids))
            .order_by(
                ModuleCard.module_id.asc(),
                ModuleCard.card_order.asc(),
                ModuleCard.id.asc(),
            )
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_families_created_since(
        self,
        since: datetime,
        *,
        tenant_id: UUID | None = None,
    ) -> list[ModuleFamily]:
        published_module = aliased(Module)
        stmt = (
            select(ModuleFamily)
            .where(
                ModuleFamily.created_at > since,
                exists(
                    select(published_module.id).where(
                        published_module.module_family_id == ModuleFamily.id,
                        published_module.lifecycle_status == "published",
                        is_training_module_family(published_module),
                    )
                ),
            )
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
            .where(
                Module.lifecycle_status == "published",
                Module.updated_at > since,
                is_training_module_family(),
            )
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
        assignable_only: bool = False,
        tenant_id: UUID | None = None,
    ) -> list[tuple[Module, float]]:
        """Semantic search via the configured ``VectorStore``, then hydrate modules."""
        filters: dict[str, object] = {"lifecycle_status": "published"}
        if assignable_only:
            filters["assignable_only"] = True
        if tenant_id is not None:
            filters["tenant_id"] = tenant_id

        store = get_vector_store(self._session)
        matches = await store.search(
            MODULES_COLLECTION,
            query_vector,
            top_k=limit,
            filters=filters,
        )
        if not matches:
            return []

        ordered_ids = [UUID(match["id"]) for match in matches]
        distance_by_id = {UUID(match["id"]): float(match["distance"]) for match in matches}
        rows = (await self._session.execute(select(Module).where(Module.id.in_(ordered_ids)))).scalars().all()
        modules_by_id = {module.id: module for module in rows}
        return [
            (modules_by_id[module_id], distance_by_id[module_id])
            for module_id in ordered_ids
            if module_id in modules_by_id
        ]

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
            is_training_module_family(),
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
            .where(
                Module.lifecycle_status.in_(["draft", "published"]),
                is_training_module_family(),
            )
            .subquery()
        )
        stmt = select(Module).join(rank_sq, Module.id == rank_sq.c.id).where(rank_sq.c.rn == 1)
        result = await self._session.execute(stmt)
        modules = list(result.scalars().all())
        if not modules:
            return []
        module_ids = [mod.id for mod in modules]
        card_counts_stmt = (
            select(ModuleCard.module_id, func.count(ModuleCard.id))
            .where(ModuleCard.module_id.in_(module_ids))
            .group_by(ModuleCard.module_id)
        )
        card_counts = {row[0]: row[1] for row in (await self._session.execute(card_counts_stmt)).all()}
        return [mod for mod in modules if card_counts.get(mod.id, 0) > 0]

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
            .where(Module.lifecycle_status == "published", tenant_filter, is_training_module_family())
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
        stmt = select(Module).where(Module.lifecycle_status == "published", is_training_module_family())
        if tenant_id is not None:
            stmt = stmt.where(tenant_scope_filter(Module.tenant_id, tenant_id))
        stmt = stmt.order_by(Module.published_at.desc().nullslast(), Module.id.asc()).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
