"""Shared constants and helpers for ingestion run state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from platform_service.db.models.ingestion_run import IngestionRunStep

STAGE_EXTRACT = "extract"
STAGE_MODULE_IDENTIFY = "module_identify"
STAGE_CARD_DRAFT = "card_draft"
STAGE_QUIZ_GENERATION = "quiz_generation"
STAGE_EMBEDDING_GENERATION = "embedding_generation"
STAGE_SEARCH_METADATA_GENERATION = "search_metadata_generation"
STAGE_CARD_SEARCH_METADATA_GENERATION = "card_search_metadata_generation"
STAGE_GAP_CLASSIFICATION = "gap_classification"
STAGE_TRIGGER_BINDING = "trigger_binding"
STAGE_CROSS_SOURCE_FUSION = "cross_source_fusion"
STAGE_THUMBNAIL = "thumbnail"

PIPELINE_STAGES = (STAGE_EXTRACT, STAGE_MODULE_IDENTIFY, STAGE_CARD_DRAFT)
POST_PUBLISH_STAGES = (
    STAGE_QUIZ_GENERATION,
    STAGE_CARD_SEARCH_METADATA_GENERATION,
    STAGE_SEARCH_METADATA_GENERATION,
    STAGE_TRIGGER_BINDING,
    STAGE_EMBEDDING_GENERATION,
    STAGE_GAP_CLASSIFICATION,
)
ALL_STAGES = PIPELINE_STAGES + POST_PUBLISH_STAGES + (STAGE_CROSS_SOURCE_FUSION, STAGE_THUMBNAIL)

FUSION_RUN_TYPE = "cross_source_fusion"

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_PARTIALLY_SUCCEEDED = "partially_succeeded"

BATCH_QUEUED = "queued"
BATCH_RUNNING = "running"
BATCH_SUCCEEDED = "succeeded"
BATCH_FAILED = "failed"
BATCH_PARTIALLY_SUCCEEDED = "partially_succeeded"

_ACTIVE_RUN_STATUSES = frozenset({RUN_QUEUED, RUN_RUNNING})
_TERMINAL_RUN_STATUSES = frozenset({RUN_SUCCEEDED, RUN_FAILED, RUN_PARTIALLY_SUCCEEDED})

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_AWAITING_INPUT = "awaiting_input"

_TERMINAL_STEP_STATUSES = frozenset({STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED})

_PIPELINE_CLAIM_KEY = "_pipeline_claim"
_DEFAULT_CLAIM_STALE_SECONDS = 6 * 60 * 60


class ConcurrentRunError(Exception):
    """Raised when starting a new run while another is already running for
    the same source_document_id."""

    def __init__(self, source_document_id: UUID, existing_run_id: UUID) -> None:
        super().__init__(
            f"source_document {source_document_id} already has an active run "
            f"({existing_run_id}); refuse to start a second concurrent run"
        )
        self.source_document_id = source_document_id
        self.existing_run_id = existing_run_id


class ConcurrentFusionRunError(Exception):
    """Raised when a fusion run is already active for an overlapping document set."""

    def __init__(self, source_document_id: UUID, existing_run_id: UUID) -> None:
        super().__init__(
            f"source_document {source_document_id} already participates in an active "
            f"fusion run ({existing_run_id}); refuse to start a second concurrent fusion"
        )
        self.source_document_id = source_document_id
        self.existing_run_id = existing_run_id


def now_utc() -> datetime:
    return datetime.now(UTC)


def as_error_object(error_jsonb: Any) -> dict[str, Any]:
    """Coerce ``error_jsonb`` to a dict.

    Run/batch ``error_jsonb`` is typed as a JSON object, but Postgres ``||``
    on jsonb coerces non-objects into arrays when merging claim metadata.
    Callers must tolerate legacy or corrupted non-object values.
    """
    return error_jsonb if isinstance(error_jsonb, dict) else {}


def terminal_run_status_from_steps(
    steps: list[IngestionRunStep],
) -> tuple[str, dict | None]:
    """Derive terminal run status from all step rows for a pipeline pass."""
    failed_stages: list[str] = []
    draft_failures = 0
    drafts_produced = 0

    for step in steps:
        if step.stage == STAGE_THUMBNAIL:
            # Thumbnail is best-effort; never drives run terminal status.
            continue
        if step.stage == STAGE_CARD_DRAFT:
            if step.status == STEP_FAILED:
                draft_failures += 1
            elif step.status == STEP_SUCCEEDED:
                drafts_produced += 1
        if step.status != STEP_FAILED:
            continue
        failed_stages.append(step.stage)

    if not failed_stages:
        return RUN_SUCCEEDED, None

    error: dict = {"failed_stages": failed_stages}
    if draft_failures:
        error["failed_stage"] = STAGE_CARD_DRAFT
        error["draft_failures"] = draft_failures
        error["drafts_produced"] = drafts_produced
    elif len(failed_stages) == 1:
        error["failed_stage"] = failed_stages[0]
    return RUN_PARTIALLY_SUCCEEDED, error


def rollup_batch_status(run_statuses: list[str]) -> str:
    """Derive batch status from child ingestion_run statuses (incl. fusion)."""
    if not run_statuses:
        return BATCH_QUEUED
    if any(s == RUN_RUNNING for s in run_statuses):
        return BATCH_RUNNING
    if any(s == RUN_QUEUED for s in run_statuses):
        return BATCH_QUEUED
    terminals = [s for s in run_statuses if s in _TERMINAL_RUN_STATUSES]
    if not terminals:
        return BATCH_QUEUED
    if all(s == RUN_SUCCEEDED for s in terminals):
        return BATCH_SUCCEEDED
    if all(s == RUN_FAILED for s in terminals):
        return BATCH_FAILED
    return BATCH_PARTIALLY_SUCCEEDED
