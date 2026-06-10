"""Module repository — admin dashboard reads + edits.

Per `docs/ARCHITECTURE_RESET.md`. The W-6 reviewer-queue surface was
deleted; this repository serves the dashboard endpoints in
`api/admin_modules.py`. Cards are stored inline on `module.module_json`,
so reads compose the runtime payload from one row + a JOIN to
`module_quiz_question`. There are no per-card row queries.

Method conventions:
- Reads return Pydantic-friendly dicts so the FastAPI handlers can return
  them directly without a separate response-model conversion step.
- Writes flush only — the calling endpoint decides commit boundaries.
- Versioning: edits create a new `module` row in the same family with
  `version = current_version + 1`. The previous row stays as
  `lifecycle_status='published'` until the new one writes its own
  `published_at`; `module_family.current_published_module_id` always points
  at the latest version.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.db.models.module_behavioural_gap import ModuleBehaviouralGap
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.models.module_quiz_question import ModuleQuizQuestion
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.db.tenant_scope import tenant_scope_filter

# Sentinel: caller did not supply thumbnail_storage_path (copy forward on version bump).
_THUMBNAIL_UNSET: object = object()


def _slugify(text: str) -> str:
    """Crude slug for module_code derivation. Bangla survives via raw chars."""
    cleaned = re.sub(r"\s+", "-", (text or "").strip().lower())
    cleaned = re.sub(r"[^\w\-]+", "", cleaned, flags=re.UNICODE)
    return cleaned[:80] or "module"


def _gap_code_for_module(module_id: UUID) -> str:
    return f"module_primary_gap_{str(module_id).replace('-', '_')}"


class ModuleNotFoundError(Exception):
    """Raised when a requested module does not exist (or has been retired
    and the caller asked to exclude retired)."""

    def __init__(self, module_id: UUID) -> None:
        super().__init__(f"module {module_id} not found")
        self.module_id = module_id


class ModuleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Reads ───────────────────────────────────────────────────────────

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
        limit: int = 50,
        offset: int = 0,
    ) -> list[Module]:
        """Filterable module list. Default excludes retired modules (callers
        wanting them must pass status='retired' explicitly).

        full_text_query runs against title_bn / title_en / description_bn —
        Postgres ILIKE for now, tsvector index can be added later if
        needed.

        latest_version_only collapses each family to its highest-version
        row that matches the filters. Useful for the reviewer dashboard
        listing where multiple versions of the same family would clutter
        the view (e.g. v1 deprecated + v2 published showing as two rows).
        """
        stmt = select(Module)
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
            # `quality_flags_jsonb IS NOT NULL AND != '{}'` — empty-dict
            # rows are functionally "no flags" and should not match.
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
            pattern = f"%{full_text_query}%"
            stmt = stmt.where(
                or_(
                    Module.title_bn.ilike(pattern),
                    Module.title_en.ilike(pattern),
                    Module.description_bn.ilike(pattern),
                )
            )
        if latest_version_only:
            # Keep only the highest-version row per family, after filters.
            # Window-function subquery so the outer ORDER BY + LIMIT/OFFSET
            # continue to work normally.
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

    async def list_modules_by_ids(self, module_ids: list[UUID]) -> list[Module]:
        if not module_ids:
            return []
        stmt = select(Module).where(Module.id.in_(module_ids))
        return list((await self._session.execute(stmt)).scalars().all())

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
        """Cosine-similarity search over `module.embedding`. Returns the
        top-k modules and their distance scores (lower = closer). Skips
        modules without an embedding (post-publish worker not run yet).
        """
        # `Module.embedding.cosine_distance(...)` is the pgvector adapter's
        # ORM-friendly accessor; emits `embedding <=> :vec` SQL with proper
        # vector-typed bind. Avoids the raw-SQL string-literal cast trick.
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
        """Published modules linked to any of ``gap_ids`` via junction table."""
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
        """Deprecated alias — use ``list_published_modules_for_gap_ids``."""
        return await self.list_published_modules_for_gap_ids(
            gap_ids=gap_ids,
            tenant_id=tenant_id,
        )

    async def list_active_modules_for_merge(self) -> list[Module]:
        """Non-retired modules for Stage 2-draft merge similarity.

        Returns the highest-version row per `module_family_id` among
        `draft` and `published` rows. Rows with no cards in `module_json`
        are excluded.
        """
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
        """True when the family already has a draft row (reviewer edit in flight)."""
        stmt = select(Module.id).where(
            Module.module_family_id == module_family_id,
            Module.lifecycle_status == "draft",
        )
        if exclude_module_id is not None:
            stmt = stmt.where(Module.id != exclude_module_id)
        stmt = stmt.limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def list_recent_published_one_per_family(
        self,
        *,
        tenant_id: UUID,
        limit: int = 5,
    ) -> list[Module]:
        """One published row per `module_family_id` (latest `created_at` in
        family), ordered by `created_at` descending, limited — for gap-suggestion
        fallback."""
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

    # ── Writes ──────────────────────────────────────────────────────────

    async def create_module(
        self,
        *,
        title_bn: str,
        title_en: str | None = None,
        description_bn: str | None = None,
        description_en: str | None = None,
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
        """Create a new module family and a new draft module row."""
        proposed_title = title_en or title_bn
        slug = _slugify(proposed_title)
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

        # Create module row in draft state
        new_module = Module(
            module_family_id=family.id,
            version=1,
            title_bn=title_bn,
            title_en=title_en,
            description_bn=description_bn,
            description_en=description_en,
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
            # If no primary gap, auto-create a default gap for this module
            # and link it as primary.
            gap = BehaviouralGap(
                gap_code=_gap_code_for_module(new_module.id),
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
        title_bn: str | None = None,
        title_en: str | None = None,
        description_bn: str | None = None,
        module_json: dict[str, Any] | None = None,
        visibility_window: Any | None = None,
        thumbnail_storage_path: str | None | object = _THUMBNAIL_UNSET,
        editor_id: UUID | None = None,
    ) -> Module:
        """Create a new version of the module with the supplied edits.

        We never mutate a published row in place — every edit produces a
        new `module` row with `version = previous + 1`, copying forward
        any fields the caller did not touch. The previous row is then
        marked `lifecycle_status='retired'` (with `deprecated_at`) so the
        dashboard's default `?status` filter (which hides retired) shows
        only the new version. Without this retire step every edit would
        double the row count for the family on the dashboard. The family
        pointer is updated to the new row.

        Callers that just want to flip `clinically_reviewed` should use
        `set_clinically_reviewed` instead — that's a metadata flip, not a
        content change, and does not version-bump.
        """
        current = await self._session.get(Module, module_id)
        if current is None or current.lifecycle_status == "retired":
            raise ModuleNotFoundError(module_id)
        next_version = current.version + 1
        if thumbnail_storage_path is _THUMBNAIL_UNSET:
            next_thumbnail = current.thumbnail_storage_path
        else:
            next_thumbnail = thumbnail_storage_path

        new_module = Module(
            module_family_id=current.module_family_id,
            version=next_version,
            title_bn=title_bn if title_bn is not None else current.title_bn,
            title_en=title_en if title_en is not None else current.title_en,
            description_bn=description_bn if description_bn is not None else current.description_bn,
            description_en=current.description_en,
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

        # Do not retire the previous published version yet, since the new one is only a draft.
        # It will be retired when the draft is clinically reviewed (published).

        # We still want the family to point to the new draft so the dashboard
        # edit flow finds the latest drafted version by default.
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
        """Flip `clinically_reviewed` without version-bumping. Records the
        reviewer + timestamp."""
        module = await self._session.get(Module, module_id)
        if module is None or module.lifecycle_status == "retired":
            raise ModuleNotFoundError(module_id)
        module.clinically_reviewed = flag
        module.clinically_reviewed_at = datetime.now(UTC) if flag else None
        module.clinically_reviewed_by = reviewer_id if flag else None

        if flag:
            module.lifecycle_status = "published"
            module.published_at = datetime.now(UTC)

            # Update family pointer
            family = await self._session.get(ModuleFamily, module.module_family_id)
            if family is not None:
                family.current_published_module_id = module.id

            # Retire older versions in the same family so they fall out of
            # the default dashboard list.
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
        """Set or clear the visibility_window range. None clears."""
        module = await self._session.get(Module, module_id)
        if module is None or module.lifecycle_status == "retired":
            raise ModuleNotFoundError(module_id)
        module.visibility_window = window
        await self._session.flush()
        return module

    async def retire_module(self, module_id: UUID) -> Module:
        """Soft-delete: lifecycle_status → retired. Still readable via the
        list with status='retired' filter; no longer surfaced to runtime."""
        module = await self._session.get(Module, module_id)
        if module is None:
            raise ModuleNotFoundError(module_id)
        module.lifecycle_status = "retired"
        module.deprecated_at = datetime.now(UTC)
        # Also clear the family pointer so the dashboard's "current"
        # module-per-family lookup doesn't return a retired row.
        family = await self._session.get(ModuleFamily, module.module_family_id)
        if family is not None and family.current_published_module_id == module.id:
            # Find the next-most-recent published version (if any).
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

    # ── Aggregates / pagination helpers ─────────────────────────────────

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


__all__ = ["ModuleRepository", "ModuleNotFoundError"]
