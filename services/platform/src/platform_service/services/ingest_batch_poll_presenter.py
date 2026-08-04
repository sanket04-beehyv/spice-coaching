"""Build tree-shaped ingest batch poll payloads for the admin UI."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.config import get_settings
from platform_service.db.models.ingestion_run import IngestionRun, IngestionRunStep
from platform_service.db.models.module_candidate_draft import ModuleCandidateDraft
from platform_service.db.models.source_document import SourceDocument
from platform_service.db.repositories.module_candidate_repository import (
    ModuleCandidateRepository,
)
from platform_service.services.ingest_progress_catalog import (
    candidate_catalog_entry,
    catalog_entry,
    chunk_catalog_entry,
)
from platform_service.services.ingestion_run_presenter import IngestionRunPresenter
from platform_service.services.run_state.constants import as_error_object
from platform_service.services.run_state.steps import is_module_identify_chunk_step
from platform_service.services.run_state_service import (
    POST_PUBLISH_STAGES,
    RUN_RUNNING,
    STAGE_CARD_DRAFT,
    STAGE_EXTRACT,
    STAGE_MODULE_IDENTIFY,
    STAGE_THUMBNAIL,
    STEP_AWAITING_INPUT,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    RunStateService,
)

_PREFIX_STAGE_ORDER = (STAGE_THUMBNAIL, STAGE_EXTRACT)
_SHARED_STAGE_ORDER = (*_PREFIX_STAGE_ORDER, STAGE_MODULE_IDENTIFY)
_CANDIDATE_STAGE_ORDER = (STAGE_CARD_DRAFT, *POST_PUBLISH_STAGES)


def _document_label(doc: SourceDocument | None) -> str:
    if doc is None:
        return ""
    filename = (doc.original_filename or "").strip()
    if filename:
        return filename
    return doc.title


def _candidate_id_from_step(step: IngestionRunStep) -> str | None:
    summary = step.input_summary_jsonb or {}
    raw = summary.get("candidate_id")
    return str(raw) if raw else None


def _step_node(step: IngestionRunStep) -> dict[str, Any]:
    poll = IngestionRunPresenter.step_to_poll_dict(step)
    activity = poll.get("activity")
    catalog_activity = activity if isinstance(activity, str) else None
    title, description = catalog_entry(step.stage, activity=catalog_activity)
    node: dict[str, Any] = {
        "key": step.stage,
        "title": title,
        "description": description,
        "status": step.status,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "completed_at": step.completed_at.isoformat() if step.completed_at else None,
        "error": step.error_jsonb,
        "error_code": step.error_code,
        "error_message": step.error_message,
        "children": [],
    }
    if activity:
        node["activity"] = activity
    if poll.get("fusion") is True:
        node["fusion"] = True
    if "published_module_merge" in poll:
        node["published_module_merge"] = poll["published_module_merge"]
    if step.input_summary_jsonb is not None:
        node["input_summary"] = step.input_summary_jsonb
    if step.output_summary_jsonb is not None:
        # Strip bulky card stashes from poll; worker still reads full step jsonb.
        output = dict(step.output_summary_jsonb)
        output.pop("new_cards", None)
        output.pop("merged_cards", None)
        node["output_summary"] = output
    return node


def _sort_steps(steps: list[IngestionRunStep], order: tuple[str, ...]) -> list[IngestionRunStep]:
    rank = {stage: idx for idx, stage in enumerate(order)}
    return sorted(
        steps,
        key=lambda s: (
            rank.get(s.stage, len(order)),
            s.started_at.timestamp() if s.started_at else 0.0,
            str(s.id),
        ),
    )


def _chunk_id_from_step(step: IngestionRunStep) -> str | None:
    summary = step.input_summary_jsonb or {}
    raw = summary.get("chunk_id")
    return str(raw) if raw else None


def _chunk_node(step: IngestionRunStep) -> dict[str, Any]:
    chunk_id = _chunk_id_from_step(step) or "chunk"
    title, description = chunk_catalog_entry(chunk_id)
    node: dict[str, Any] = {
        "key": "chunk",
        "title": title,
        "description": description,
        "status": step.status,
        "chunk_id": chunk_id,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "completed_at": step.completed_at.isoformat() if step.completed_at else None,
        "error": step.error_jsonb,
        "error_code": step.error_code,
        "error_message": step.error_message,
        "children": [],
    }
    if step.input_summary_jsonb is not None:
        node["input_summary"] = step.input_summary_jsonb
    if step.output_summary_jsonb is not None:
        node["output_summary"] = step.output_summary_jsonb
    return node


def _home_chunk_id(cand: ModuleCandidateDraft | None) -> str | None:
    """Pipeline provenance home: first source_chunk_ids entry, if any."""
    if cand is None:
        return None
    ids = cand.source_chunk_ids
    if not ids:
        return None
    first = ids[0]
    if first is None or first == "":
        return None
    return str(first)


def build_run_tree(
    *,
    steps: list[IngestionRunStep],
    candidates: list[ModuleCandidateDraft],
) -> list[dict[str, Any]]:
    """Assemble progressed-only nodes for one pipeline (or fusion) run."""
    prefix = [s for s in steps if s.stage in _PREFIX_STAGE_ORDER]
    nodes = [_step_node(s) for s in _sort_steps(prefix, _PREFIX_STAGE_ORDER)]

    by_candidate: dict[str, list[IngestionRunStep]] = {}
    for step in steps:
        if step.stage not in _CANDIDATE_STAGE_ORDER:
            continue
        cand_id = _candidate_id_from_step(step)
        if cand_id is None:
            continue
        by_candidate.setdefault(cand_id, []).append(step)

    candidates_by_id = {str(c.id): c for c in candidates}
    # Preserve candidate emission order; append orphan candidate_ids from steps.
    ordered_ids = [str(c.id) for c in candidates if str(c.id) in by_candidate]
    for cand_id in by_candidate:
        if cand_id not in ordered_ids:
            ordered_ids.append(cand_id)

    identify_all = [s for s in steps if s.stage == STAGE_MODULE_IDENTIFY]
    parent_steps = [s for s in identify_all if not is_module_identify_chunk_step(s)]
    chunk_steps = [s for s in identify_all if is_module_identify_chunk_step(s)]
    chunk_nodes = [_chunk_node(s) for s in _sort_steps(chunk_steps, (STAGE_MODULE_IDENTIFY,))]
    chunks_by_id = {str(n["chunk_id"]): n for n in chunk_nodes}

    for cand_id in ordered_ids:
        cand = candidates_by_id.get(cand_id)
        home = _home_chunk_id(cand)
        chunk_node = chunks_by_id.get(home) if home is not None else None
        if chunk_node is None:
            # Missing lineage or unknown chunk: omit from the tree.
            continue
        proposed = cand.proposed_title if cand is not None else ""
        title, description = candidate_catalog_entry(proposed)
        child_steps = _sort_steps(by_candidate[cand_id], _CANDIDATE_STAGE_ORDER)
        branch = {
            "key": "candidate",
            "title": title,
            "description": description,
            "status": _candidate_branch_status(child_steps),
            "candidate_id": cand_id,
            "proposed_title": proposed or None,
            "started_at": child_steps[0].started_at.isoformat() if child_steps[0].started_at else None,
            "completed_at": None,
            "error": None,
            "children": [_step_node(s) for s in child_steps],
        }
        chunk_node["children"].append(branch)

    for chunk_node in chunk_nodes:
        identify_status = str(chunk_node["status"])
        nested_statuses = [str(b["status"]) for b in chunk_node["children"]]
        chunk_node["status"] = _rollup_statuses([identify_status, *nested_statuses])

    identify_node: dict[str, Any] | None = None
    if parent_steps:
        identify_node = _step_node(_sort_steps(parent_steps, (STAGE_MODULE_IDENTIFY,))[0])
    elif chunk_nodes:
        title, description = catalog_entry(STAGE_MODULE_IDENTIFY)
        identify_node = {
            "key": STAGE_MODULE_IDENTIFY,
            "title": title,
            "description": description,
            "status": STEP_PENDING,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "children": [],
        }

    if identify_node is not None:
        identify_node["children"] = chunk_nodes
        if chunk_nodes:
            identify_node["status"] = _rollup_statuses([str(n["status"]) for n in chunk_nodes])
        nodes.append(identify_node)

    # Fusion / other non-shared stages without candidate_id (e.g. cross_source_fusion).
    covered = set(_SHARED_STAGE_ORDER) | set(_CANDIDATE_STAGE_ORDER)
    extras = [s for s in steps if s.stage not in covered]
    for step in sorted(
        extras,
        key=lambda s: (s.started_at.timestamp() if s.started_at else 0.0, str(s.id)),
    ):
        nodes.append(_step_node(step))

    return nodes


def _rollup_statuses(statuses: list[str]) -> str:
    if any(s == STEP_RUNNING for s in statuses):
        return STEP_RUNNING
    if any(s == STEP_AWAITING_INPUT for s in statuses):
        return STEP_AWAITING_INPUT
    if any(s == STEP_PENDING for s in statuses):
        return STEP_PENDING
    if statuses and all(s == STEP_SUCCEEDED for s in statuses):
        return STEP_SUCCEEDED
    if statuses and all(s == STEP_FAILED for s in statuses):
        return STEP_FAILED
    if statuses and all(s in (STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED) for s in statuses):
        if any(s == STEP_FAILED for s in statuses):
            return "partially_succeeded"
        return STEP_SUCCEEDED
    return statuses[-1] if statuses else STEP_PENDING


def _candidate_branch_status(steps: list[IngestionRunStep]) -> str:
    return _rollup_statuses([s.status for s in steps])


def _retry_targets_for_run(
    *,
    batch_id: UUID,
    run: IngestionRun,
    steps: list[IngestionRunStep],
    blocked_by_active_claim: bool,
) -> list[dict[str, Any]]:
    """Build POST bodies for failed steps the retry API would accept (not noop)."""
    if blocked_by_active_claim:
        return []

    targets: list[dict[str, Any]] = []
    for step in steps:
        if step.status != STEP_FAILED:
            continue
        entry: dict[str, Any] = {
            "run_id": str(run.id),
            "stage": step.stage,
        }
        summary = step.input_summary_jsonb or {}
        candidate_id = summary.get("candidate_id")
        if candidate_id:
            entry["candidate_id"] = str(candidate_id)
        chunk_id = summary.get("chunk_id")
        if chunk_id:
            entry["chunk_id"] = str(chunk_id)
        targets.append(entry)
    return targets


class IngestBatchPollPresenter:
    """JSON payload for ``GET /admin/ingest/batches/{batch_id}``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._state = RunStateService(session)
        self._candidate_repo = ModuleCandidateRepository(session)

    async def present_batch(self, batch_id: UUID) -> dict[str, Any] | None:
        batch = await self._state.refresh_batch_status(batch_id)
        if batch is None:
            return None

        runs = await self._state.list_runs_for_batch(batch_id)
        pipeline_runs = [r for r in runs if not RunStateService.is_fusion_run(r)]
        fusion_runs = [r for r in runs if RunStateService.is_fusion_run(r)]

        doc_ids = list({r.source_document_id for r in pipeline_runs})
        docs_by_id: dict[UUID, SourceDocument] = {}
        if doc_ids:
            docs_result = await self._session.execute(
                select(SourceDocument).where(SourceDocument.id.in_(doc_ids))
            )
            docs_by_id = {d.id: d for d in docs_result.scalars().all()}

        sources: list[dict[str, Any]] = []
        retries: list[dict[str, Any]] = []
        for run in pipeline_runs:
            steps = await self._state.list_steps(run.id)
            candidates = await self._candidate_repo.list_candidates_for_run(run.id)
            blocked = run.status == RUN_RUNNING and self._state.has_active_pipeline_claim(run)
            sources.append(
                {
                    "source_document_id": str(run.source_document_id),
                    "run_id": str(run.id),
                    "document_label": _document_label(docs_by_id.get(run.source_document_id)),
                    "status": run.status,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                    "error": run.error_jsonb,
                    "nodes": build_run_tree(steps=steps, candidates=candidates),
                }
            )
            retries.extend(
                _retry_targets_for_run(
                    batch_id=batch_id,
                    run=run,
                    steps=steps,
                    blocked_by_active_claim=blocked,
                )
            )

        if fusion_runs:
            fusion_run = fusion_runs[-1]
            steps = await self._state.list_steps(fusion_run.id)
            candidates = await self._candidate_repo.list_candidates_for_run(fusion_run.id)
            fusion_title, fusion_description = catalog_entry("fusion")
            fusion_payload: dict[str, Any] = {
                "key": "fusion",
                "title": fusion_title,
                "description": fusion_description,
                "run_id": str(fusion_run.id),
                "status": fusion_run.status,
                "started_at": fusion_run.started_at.isoformat() if fusion_run.started_at else None,
                "completed_at": fusion_run.completed_at.isoformat() if fusion_run.completed_at else None,
                "error": fusion_run.error_jsonb,
                "source_document_ids": as_error_object(fusion_run.error_jsonb).get("source_document_ids"),
                "nodes": build_run_tree(steps=steps, candidates=candidates),
            }
            retries.extend(
                _retry_targets_for_run(
                    batch_id=batch_id,
                    run=fusion_run,
                    steps=steps,
                    blocked_by_active_claim=(
                        fusion_run.status == RUN_RUNNING and self._state.has_active_pipeline_claim(fusion_run)
                    ),
                )
            )
        else:
            fusion_payload = None

        payload: dict[str, Any] = {
            "batch_id": str(batch.id),
            "status": batch.status,
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
            "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
            "error": batch.error_jsonb,
            "sources": sources,
            "retry_url": (
                get_settings().api_path(f"/admin/ingest/batches/{batch_id}/retry") if retries else None
            ),
        }
        if fusion_payload is not None:
            payload["fusion"] = fusion_payload
        return payload
