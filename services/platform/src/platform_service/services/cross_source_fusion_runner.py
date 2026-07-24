"""Stage 2b orchestrator — load candidates, fuse, draft, publish, retire.

This module wraps the pure-compute `CrossSourceFuser` with the DB-side
glue needed to make fusion an end-to-end operation:

1. Load every candidate from the latest non-fusion ingestion run for
   each source_document_id in the request.
2. Call `CrossSourceFuser.fuse(candidates)` to identify cross-source
   pairings (e.g., BRAC clinical "ANC counselling" + UHIS workflow
   "Conducting ANC visits" → one fused unit).
3. Persist each fusion group as a new `module_candidate_draft` row
   anchored on a freshly-created "fusion" `ingestion_run`. The fused
   row's `source_provenance` is the union of constituents'; its
   `quality_flags_jsonb` carries `merge_lineage` for audit.
4. Draft each fused candidate via the existing `StageDOrchestrator`,
   producing modules whose `source_document_ids` array spans the
   constituents' source docs and whose cards cite blocks from each
   source (drafter v2's cross-source coverage rule, see
   card_drafter_prompt.py).
5. Retire constituent modules: for every constituent candidate id,
   find the published module whose primary-locale title matches the candidate's
   `proposed_title` AND whose `source_document_ids` overlaps the
   candidate's source — set `lifecycle_status = 'retired'`. The
   Android client filters on `published`, so retired constituents
   stop surfacing without a schema migration. Heuristic match (no
   candidate→module FK exists today); good enough for the BRAC+UHIS
   pilot and trivially upgradable to an FK column later.

The unfused candidates (most of the input — single-source candidates
with no cross-source counterpart) are left untouched. Their
already-published per-source modules continue to ship. Only the
constituents of actual fusion groups get retired.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.db.base import SessionLocal
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.cross_source_fuser import CrossSourceFuser, CrossSourceFuserResult, FusionGroup
from platform_service.services.fusion_candidate_loader import load_fusion_candidates
from platform_service.services.fusion_draft_orchestrator import FusionDraftOrchestrator
from platform_service.services.fusion_retire_policy import FusionRetirePolicy
from platform_service.services.ingestion_cardinality import resolve_from_source_documents
from platform_service.services.run_state_service import (
    RUN_FAILED,
    RUN_SUCCEEDED,
    STAGE_CARD_DRAFT,
    STAGE_CROSS_SOURCE_FUSION,
    RunStateService,
)
from platform_service.workers.stage_d_draft import StageDOrchestrator

logger = logging.getLogger(__name__)


@dataclass
class FusionRunSummary:
    """Per-fusion-call audit record. Returned to the API caller for
    immediate visibility; the same data is observable later via the
    fusion ingestion_run row + persisted candidates' merge_lineage."""

    fusion_run_id: UUID
    input_candidate_count: int
    fusion_group_count: int
    fused_modules_published: int
    fused_modules_failed: int
    fused_modules_with_coverage_warning: int
    constituents_retired: int
    drafts: list[dict[str, Any]] = field(default_factory=list)


class CrossSourceFusionRunner:
    """Orchestrates the full Stage 2b → Stage 3 → publish flow for a
    multi-source workspace."""

    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        fuser: CrossSourceFuser | None = None,
        stage_d: StageDOrchestrator | None = None,
        draft_orchestrator: FusionDraftOrchestrator | None = None,
        retire_policy: FusionRetirePolicy | None = None,
    ) -> None:
        self._session = session
        self._fuser = fuser or CrossSourceFuser()
        self._stage_d = stage_d
        self._draft_orchestrator = draft_orchestrator
        self._retire_policy = retire_policy

    @classmethod
    async def run_staged(cls, source_document_ids: list[UUID], **kwargs: Any) -> FusionRunSummary:
        """Run fusion releasing the DB connection before LLM-heavy work."""
        runner = cls(session=None, **kwargs)
        return await runner.run(source_document_ids)

    def _draft_orchestrator_for(self, session: AsyncSession) -> FusionDraftOrchestrator:
        if self._draft_orchestrator is not None:
            return self._draft_orchestrator
        stage_d = self._stage_d or StageDOrchestrator(session)
        return FusionDraftOrchestrator(session, stage_d)

    def _retire_policy_for(self, session: AsyncSession) -> FusionRetirePolicy:
        return self._retire_policy or FusionRetirePolicy(session)

    async def run(self, source_document_ids: list[UUID]) -> FusionRunSummary:
        if len(source_document_ids) < 2:
            raise ValueError(
                f"cross-source fusion requires ≥2 source_document_ids; got {len(source_document_ids)}"
            )

        if self._session is not None:
            return await self._run_bound(source_document_ids)

        candidates, candidates_by_id, fusion_run_id, fuse_step_id = await self._prepare_fusion_run(
            source_document_ids
        )

        try:
            fusion_result = await self._fuser.fuse(candidates)
        except Exception as exc:
            logger.exception("Stage 2b fuser failed for fusion_run %s", fusion_run_id)
            async with SessionLocal() as session:
                run_state = RunStateService(session)
                await run_state.fail_step(
                    fuse_step_id,
                    error={"type": type(exc).__name__, "message": str(exc)[:500]},
                )
                await run_state.complete_run(fusion_run_id, status=RUN_FAILED)
                await session.commit()
            raise

        return await self._finalize_fusion_run(
            source_document_ids=source_document_ids,
            candidates=candidates,
            candidates_by_id=candidates_by_id,
            fusion_run_id=fusion_run_id,
            fuse_step_id=fuse_step_id,
            fusion_result=fusion_result,
        )

    async def _run_bound(self, source_document_ids: list[UUID]) -> FusionRunSummary:
        """Run using the constructor-bound session (tests and legacy callers)."""
        session = self._session
        assert session is not None

        candidates, candidates_by_id, fusion_run_id, fuse_step_id = await self._prepare_fusion_run(
            source_document_ids,
            session=session,
        )

        try:
            fusion_result = await self._fuser.fuse(candidates)
        except Exception as exc:
            logger.exception("Stage 2b fuser failed for fusion_run %s", fusion_run_id)
            run_state = RunStateService(session)
            await run_state.fail_step(
                fuse_step_id,
                error={"type": type(exc).__name__, "message": str(exc)[:500]},
            )
            await run_state.complete_run(fusion_run_id, status=RUN_FAILED)
            await session.commit()
            raise

        return await self._finalize_fusion_run(
            source_document_ids=source_document_ids,
            candidates=candidates,
            candidates_by_id=candidates_by_id,
            fusion_run_id=fusion_run_id,
            fuse_step_id=fuse_step_id,
            fusion_result=fusion_result,
            session=session,
        )

    async def _prepare_fusion_run(
        self,
        source_document_ids: list[UUID],
        *,
        session: AsyncSession | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], UUID, UUID]:
        """Load candidates, create fusion run, and start fuse step; then release session."""
        if session is None:
            async with SessionLocal() as scoped_session:
                return await self._prepare_fusion_run(
                    source_document_ids,
                    session=scoped_session,
                )

        candidates = await self._load_candidates(source_document_ids, session=session)
        if not candidates:
            raise ValueError(
                f"no candidates found for source_document_ids={source_document_ids}; "
                "ensure each doc has a completed Stage 2a ingestion run"
            )
        candidates_by_id: dict[str, dict[str, Any]] = {c["id"]: c for c in candidates}
        logger.info(
            "Stage 2b runner: loaded %d candidates across %d source documents",
            len(candidates),
            len(source_document_ids),
        )

        run_state = RunStateService(session)
        fusion_run = await run_state.start_fusion_run(source_document_ids=source_document_ids)
        fusion_run_id = fusion_run.id
        self._draft_orchestrator_for(session).bind_fusion_run(fusion_run_id)
        await session.commit()

        fuse_step = await run_state.start_step(
            run_id=fusion_run_id,
            stage=STAGE_CROSS_SOURCE_FUSION,
            input_summary={"activity": "cross_source_fusion"},
        )
        fuse_step_id = fuse_step.id
        await session.commit()
        return candidates, candidates_by_id, fusion_run_id, fuse_step_id

    async def _finalize_fusion_run(
        self,
        *,
        source_document_ids: list[UUID],
        candidates: list[dict[str, Any]],
        candidates_by_id: dict[str, dict[str, Any]],
        fusion_run_id: UUID,
        fuse_step_id: UUID,
        fusion_result: CrossSourceFuserResult,
        session: AsyncSession | None = None,
    ) -> FusionRunSummary:
        if session is None:
            async with SessionLocal() as scoped_session:
                return await self._finalize_fusion_run(
                    source_document_ids=source_document_ids,
                    candidates=candidates,
                    candidates_by_id=candidates_by_id,
                    fusion_run_id=fusion_run_id,
                    fuse_step_id=fuse_step_id,
                    fusion_result=fusion_result,
                    session=scoped_session,
                )

        run_state = RunStateService(session)
        group_count = len(fusion_result.fusion_groups)
        await run_state.complete_step(
            fuse_step_id,
            output_summary={"fusion_group_count": group_count},
        )
        await session.commit()

        if not fusion_result.fusion_groups:
            logger.info("Stage 2b runner: no fusion groups emerged — nothing to draft")
            await run_state.complete_run(fusion_run_id, status=RUN_SUCCEEDED)
            await session.commit()
            return FusionRunSummary(
                fusion_run_id=fusion_run_id,
                input_candidate_count=len(candidates),
                fusion_group_count=0,
                fused_modules_published=0,
                fused_modules_failed=0,
                fused_modules_with_coverage_warning=0,
                constituents_retired=0,
            )

        logger.info(
            "Stage 2b runner: fusion ingestion_run %s; persisting %d fused candidates",
            fusion_run_id,
            group_count,
        )

        fused_candidate_ids: list[tuple[UUID, FusionGroup, set[str]]] = []
        for group in fusion_result.fusion_groups:
            cid = await self._persist_fused_candidate(
                fusion_run_id,
                group,
                candidates_by_id,
                session=session,
            )
            expected_sources = {
                str(candidates_by_id[str(c)]["source_document_id"]) for c in group.constituent_ids
            }
            fused_candidate_ids.append((cid, group, expected_sources))
        await session.commit()

        published = 0
        failed = 0
        coverage_warnings = 0
        drafts: list[dict[str, Any]] = []
        for fc_id, group, expected_sources in fused_candidate_ids:
            (
                module_id,
                cards_count,
                reason,
                coverage_ok,
                _draft_summary,
            ) = await self._draft_fused_candidate_in_staged_session(
                fusion_run_id=fusion_run_id,
                fc_id=fc_id,
                group=group,
                expected_sources=expected_sources,
            )
            drafts.append(
                {
                    "fused_candidate_id": str(fc_id),
                    "module_id": str(module_id) if module_id else None,
                    "cards_count": cards_count,
                    "merged_title": group.merged_title,
                    "constituent_candidate_ids": [str(cid) for cid in group.constituent_ids],
                    "insufficient_reason": reason,
                    "cross_source_coverage_ok": coverage_ok,
                }
            )
            if module_id is not None:
                published += 1
                if not coverage_ok:
                    coverage_warnings += 1
            else:
                failed += 1

        all_constituent_ids: list[UUID] = []
        for _, group, _ in fused_candidate_ids:
            all_constituent_ids.extend(group.constituent_ids)
        retired = await self._retire_policy_for(session).retire_constituent_modules(
            all_constituent_ids,
            candidates_by_id,
        )

        await run_state.maybe_finalize_ingestion_run(fusion_run_id)
        await session.commit()

        logger.info(
            "Stage 2b runner: fusion_run=%s published=%d failed=%d coverage_warnings=%d retired_constituents=%d",
            fusion_run_id,
            published,
            failed,
            coverage_warnings,
            retired,
        )
        return FusionRunSummary(
            fusion_run_id=fusion_run_id,
            input_candidate_count=len(candidates),
            fusion_group_count=len(fusion_result.fusion_groups),
            fused_modules_published=published,
            fused_modules_failed=failed,
            fused_modules_with_coverage_warning=coverage_warnings,
            constituents_retired=retired,
            drafts=drafts,
        )

    # ── internals ─────────────────────────────────────────────────────

    async def _draft_fused_candidate_in_staged_session(
        self,
        *,
        fusion_run_id: UUID,
        fc_id: UUID,
        group: FusionGroup,
        expected_sources: set[str],
    ) -> tuple[UUID | None, int, str | None, bool, dict[str, Any]]:
        """Run one LLM-bound fused draft in a fresh DB session."""
        async with SessionLocal() as draft_session:
            run_state = RunStateService(draft_session)
            draft_orchestrator = FusionDraftOrchestrator(
                draft_session,
                StageDOrchestrator(draft_session),
            )
            draft_orchestrator.bind_fusion_run(fusion_run_id)
            draft_step = await run_state.start_step(
                run_id=fusion_run_id,
                stage=STAGE_CARD_DRAFT,
                input_summary={
                    "candidate_id": str(fc_id),
                    "fusion": True,
                    "merged_title": group.merged_title,
                },
            )
            draft_step_id = draft_step.id
            await draft_session.commit()
            module_id, cards_count, reason, coverage_ok = await draft_orchestrator.draft_with_coverage(
                fc_id,
                expected_sources,
                step_id=draft_step_id,
            )
            draft_summary = {
                "candidate_id": str(fc_id),
                "module_id": str(module_id) if module_id else None,
                "cards_count": cards_count,
                "merged_title": group.merged_title,
                "insufficient_reason": reason,
                "cross_source_coverage_ok": coverage_ok,
            }
            if module_id is not None:
                await run_state.complete_step(draft_step_id, output_summary=draft_summary)
            else:
                await run_state.fail_step(
                    draft_step_id,
                    error={
                        "candidate_id": str(fc_id),
                        "insufficient_reason": reason or "draft_failed",
                    },
                )
            await draft_session.commit()
            return module_id, cards_count, reason, coverage_ok, draft_summary

    async def _load_candidates(
        self,
        doc_ids: list[UUID],
        *,
        session: AsyncSession | None = None,
    ) -> list[dict[str, Any]]:
        db = session or self._session
        assert db is not None
        return await load_fusion_candidates(db, doc_ids)

    async def _persist_fused_candidate(
        self,
        fusion_run_id: UUID,
        group: FusionGroup,
        candidates_by_id: dict[str, dict[str, Any]],
        *,
        session: AsyncSession | None = None,
    ) -> UUID:
        db = session or self._session
        assert db is not None
        """Insert one module_candidate_draft row representing the fused
        unit. source_provenance is the union of constituents'."""
        merged_prov: list[dict[str, Any]] = []
        constituent_titles: list[str] = []
        for cid in group.constituent_ids:
            c = candidates_by_id[str(cid)]
            constituent_titles.append(c["proposed_title"])
            for entry in c.get("source_provenance") or []:
                merged_prov.append(entry)

        module_type = candidates_by_id[str(group.constituent_ids[0])].get(
            "proposed_module_type", "initial_training"
        )
        domain = candidates_by_id[str(group.constituent_ids[0])].get("domain")

        quality_flags = {
            "flags": ["cross_source_fused"],
            "merge_lineage": {
                "constituent_candidate_ids": [str(cid) for cid in group.constituent_ids],
                "constituent_titles": constituent_titles,
                "pairing_rationale": group.pairing_rationale,
            },
        }

        source_doc_ids = {
            UUID(str(candidates_by_id[str(cid)]["source_document_id"]))
            for cid in group.constituent_ids
            if candidates_by_id.get(str(cid), {}).get("source_document_id")
        }
        documents = []
        if source_doc_ids:
            source_repo = SourceRepository(db)
            for doc_id in source_doc_ids:
                doc = await source_repo.get_source_document(doc_id)
                if doc is not None:
                    documents.append(doc)
        cardinality = resolve_from_source_documents(documents)
        first_constituent = candidates_by_id[str(group.constituent_ids[0])]
        estimated_card_count = (
            cardinality.target_cards
            if cardinality.target_cards is not None
            else int(first_constituent.get("estimated_card_count") or 5)
        )
        estimated_quiz_count = (
            cardinality.target_quizzes
            if cardinality.target_quizzes is not None
            else int(first_constituent.get("estimated_quiz_count") or 5)
        )

        repo = ModuleCandidateRepository(db)
        cand = await repo.create_candidate(
            ingestion_run_id=fusion_run_id,
            proposed_title=group.merged_title,
            scope_summary=group.merged_scope_summary,
            domain=domain,
            source_provenance=merged_prov,
            estimated_card_count=estimated_card_count,
            estimated_quiz_count=estimated_quiz_count,
            proposed_module_type=module_type,
            quality_flags=quality_flags,
        )
        return cand.id
