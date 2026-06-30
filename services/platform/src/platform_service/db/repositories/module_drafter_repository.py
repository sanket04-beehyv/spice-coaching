"""Stage 2 module-drafter repository.

Persists Stage 2 output: a Module row with cards inlined as a JSON array on
`module_json` (per `docs/ARCHITECTURE_RESET.md`). The Module is created
auto-published (`lifecycle_status='published'`, `clinically_reviewed=false`)
and the orchestrator enqueues post-publish embedding + quiz workers.

Quiz questions are written by the post-publish quiz worker, not here — quiz
generation runs on the published module asynchronously and writes
`module_quiz_question` rows linked via the new `module_id` FK.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.behavioural_gap import BehaviouralGap
from platform_service.db.models.module import Module
from platform_service.db.models.module_family import ModuleFamily
from platform_service.db.repositories.module_gap_repository import ModuleGapRepository
from platform_service.localized import (
    candidate_description_localized,
    migrate_legacy_card,
    primary_text,
    to_localized_string,
)
from platform_service.services.card_normalisation import project_runtime_card
from platform_service.services.module_thumbnail_service import resolve_default_module_thumbnail


def _gap_code_for_module(module_id: UUID) -> str:
    return f"module_primary_gap_{str(module_id).replace('-', '_')}"


def _slugify(text: str) -> str:
    """Crude slug for module_code derivation. Bangla survives via raw chars."""
    cleaned = re.sub(r"\s+", "-", (text or "").strip().lower())
    cleaned = re.sub(r"[^\w\-]+", "", cleaned, flags=re.UNICODE)
    return cleaned[:80] or "module"


def _first_card_primary_title(cards_payload: list[dict[str, Any]]) -> str:
    for card in cards_payload:
        migrated = migrate_legacy_card(card)
        title = primary_text(migrated.get("title"))
        if title and title.strip():
            return title.strip()
    return ""


class ModuleDrafterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_module_family(
        self, *, proposed_title: str, created_by: UUID | None = None
    ) -> ModuleFamily:
        """Look up by derived module_code; create if absent.

        On collision we append a numeric suffix (-1, -2, …) so two modules
        with the same proposed_title still get distinct families. Module
        re-versioning (same family, version+1) goes through the dashboard's
        edit endpoint, not through here.
        """
        slug = _slugify(proposed_title)
        candidate_code = slug
        attempt = 0
        while True:
            existing = await self._session.execute(
                select(ModuleFamily).where(ModuleFamily.module_code == candidate_code)
            )
            row = existing.scalar_one_or_none()
            if row is None:
                fam = ModuleFamily(module_code=candidate_code, created_by=created_by)
                self._session.add(fam)
                await self._session.flush()
                return fam
            attempt += 1
            candidate_code = f"{slug}-{attempt}"

    async def create_published_module(
        self,
        *,
        family: ModuleFamily,
        candidate: dict[str, Any],
        cards: list[dict[str, Any]],
        source_document_ids: list[UUID],
        quality_flags: dict[str, Any] | None = None,
    ) -> Module:
        """Persist a Module row with cards inlined as `module_json.cards`.

        Auto-publish path: status goes to `published` and `clinically_reviewed`
        defaults to false. The admin dashboard flips that flag once a
        clinician has signed off — it is not a publish gate.

        Also creates a dedicated `behavioural_gap` per module and sets
        `primary_gap_id` so gap-driven suggestions and quiz state updates work.

        `quality_flags` carries the advisory flags the candidate accumulated
        in Stage 2 (insufficient-source heuristic) plus any Stage 2-draft
        validator soft-warning summary. Pipeline never gates on these.
        """
        version = await self._next_version(family.id)
        # Strip per-card transient fields the LLM included for downstream
        # use (e.g., `card_family_id`, `field_flags`) but normalise their
        # keys so the runtime payload is stable.
        cards_payload = [project_runtime_card(c) for c in cards]
        module_json: dict[str, Any] = {"cards": cards_payload}

        # Title: first card primary title, else candidate proposed_title.
        proposed_title = candidate.get("proposed_title", "") or ""
        first_card_title_primary = _first_card_primary_title(cards_payload)
        primary_title = first_card_title_primary or proposed_title.strip()
        title_localized = to_localized_string(primary_title or None)
        if not primary_text(title_localized):
            raise ValueError(
                f"create_published_module: candidate has no usable title "
                f"(proposed_title={proposed_title!r}, "
                f"cards={len(cards_payload)} with no primary card title)"
            )

        thumbnail_storage_path = await resolve_default_module_thumbnail(self._session, source_document_ids)

        module = Module(
            module_family_id=family.id,
            version=version,
            title_localized=title_localized,
            description_localized=candidate.get("description_localized"),
            domain=candidate.get("domain") or get_settings().default_module_domain,
            sub_domain=candidate.get("sub_domain"),
            module_type=candidate.get("proposed_module_type", "refresher"),
            primary_gap_id=None,
            estimated_minutes=int(candidate.get("estimated_minutes", 10)),
            difficulty_level=candidate.get("difficulty_level", "moderate"),
            source_document_ids=list(source_document_ids),
            thumbnail_storage_path=thumbnail_storage_path,
            module_json=module_json,
            quality_flags_jsonb=quality_flags,
            lifecycle_status="draft",
            clinically_reviewed=False,
            published_at=None,
        )
        self._session.add(module)
        await self._session.flush()

        gap_description = primary_text(title_localized) or proposed_title or ""
        await self._create_and_link_primary_gap(module, description=gap_description)

        # Do not update family.current_published_module_id for drafts.
        # It will be updated when the module is published.

        return module

    async def create_merged_draft_module(
        self,
        *,
        matched_published: Module,
        candidate: dict[str, Any],
        cards: list[dict[str, Any]],
        source_document_ids: list[UUID],
        quality_flags: dict[str, Any] | None = None,
        match_rationale: str | None = None,
    ) -> Module:
        """New draft version in the matched published module's family.

        Reuses `primary_gap_id` from the published row. Sets
        `supersedes_module_id` to the published module being retired.
        """
        version = await self._next_version(matched_published.module_family_id)
        cards_payload = [project_runtime_card(c) for c in cards]
        module_json: dict[str, Any] = {"cards": cards_payload}

        proposed_title = candidate.get("proposed_title", "") or ""
        first_card_title_primary = _first_card_primary_title(cards_payload)
        primary_title = (
            first_card_title_primary
            or primary_text(matched_published.title_localized)
            or proposed_title.strip()
        )
        title_localized = to_localized_string(primary_title or None)
        if not primary_text(title_localized):
            raise ValueError(
                f"create_merged_draft_module: no usable title (proposed_title={proposed_title!r})"
            )

        _module_type = candidate.get("proposed_module_type", matched_published.module_type)

        merged_quality = _merge_quality_flags(
            quality_flags,
            superseded_module_id=matched_published.id,
            match_rationale=match_rationale,
        )

        thumbnail_storage_path = matched_published.thumbnail_storage_path
        if not thumbnail_storage_path:
            thumbnail_storage_path = await resolve_default_module_thumbnail(
                self._session, source_document_ids
            )

        module = Module(
            module_family_id=matched_published.module_family_id,
            version=version,
            title_localized=title_localized,
            description_localized=candidate_description_localized(candidate)
            or matched_published.description_localized,
            domain=candidate.get("domain") or matched_published.domain,
            sub_domain=candidate.get("sub_domain") or matched_published.sub_domain,
            module_type=_module_type,
            tenant_id=matched_published.tenant_id,
            primary_gap_id=matched_published.primary_gap_id,
            estimated_minutes=int(candidate.get("estimated_minutes", matched_published.estimated_minutes)),
            difficulty_level=candidate.get("difficulty_level", matched_published.difficulty_level),
            source_document_ids=list(source_document_ids),
            thumbnail_storage_path=thumbnail_storage_path,
            module_json=module_json,
            quality_flags_jsonb=merged_quality,
            lifecycle_status="draft",
            clinically_reviewed=False,
            published_at=None,
            supersedes_module_id=matched_published.id,
        )
        self._session.add(module)
        await self._session.flush()

        await ModuleGapRepository(self._session).copy_links(matched_published.id, module.id)

        family = await self._session.get(ModuleFamily, matched_published.module_family_id)
        if family is not None:
            family.current_published_module_id = module.id
        await self._session.flush()
        return module

    async def _create_and_link_primary_gap(self, module: Module, *, description: str) -> BehaviouralGap:
        gap = BehaviouralGap(
            gap_code=_gap_code_for_module(module.id),
            description=description,
            domain=module.domain,
            severity_default="moderate",
            detection_rule_jsonb={},
            status="active",
        )
        self._session.add(gap)
        await self._session.flush()
        await ModuleGapRepository(self._session).add_primary_link(module, behavioural_gap_id=gap.id)
        return gap

    # Backwards-compat alias for any caller still on the old name. New code
    # should call `create_published_module` directly.
    create_draft_module = create_published_module

    async def _next_version(self, module_family_id: UUID) -> int:
        result = await self._session.execute(
            select(Module.version)
            .where(Module.module_family_id == module_family_id)
            .order_by(Module.version.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return (row or 0) + 1


def _merge_quality_flags(
    existing: dict[str, Any] | None,
    *,
    superseded_module_id: UUID,
    match_rationale: str | None,
) -> dict[str, Any]:
    out: dict[str, Any] = dict(existing) if existing else {}
    flags = list(out.get("flags") or [])
    if "published_module_merged" not in flags:
        flags.append("published_module_merged")
    out["flags"] = flags
    out["merge_lineage"] = {
        "superseded_module_id": str(superseded_module_id),
        "match_rationale": match_rationale or "",
    }
    return out
