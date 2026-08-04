"""Admin ingest API contracts — platform → admin dashboard."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mc_contracts.enums import AssessmentMode


class ExistingIngestedSourceSummary(BaseModel):
    """A source_document that already has the same content bytes (uploaded or ingested)."""

    source_document_id: uuid.UUID
    title: str
    original_filename: str | None = None
    ingested_at: datetime
    status: str


class IngestDuplicateConflict(BaseModel):
    """One uploaded file blocked because matching content is already uploaded or ingested."""

    filename: str
    title: str
    content_sha256: str
    existing_source_documents: list[ExistingIngestedSourceSummary]


class IngestUploadedSource(BaseModel):
    """One source_document staged by ``POST /admin/ingest/upload``."""

    source_document_id: uuid.UUID
    title: str
    source_type: str
    stored_path: str
    content_domain: str
    status: str = "uploaded"


class IngestUploadResponse(BaseModel):
    """Result of ``POST /admin/ingest/upload``."""

    status: Literal["uploaded"] = "uploaded"
    sources: list[IngestUploadedSource] = Field(default_factory=list)
    skipped_duplicates: list[IngestDuplicateConflict] = Field(default_factory=list)


class IngestStartRequest(BaseModel):
    """Body for ``POST /admin/ingest`` — queue pipeline for staged source documents."""

    model_config = ConfigDict(extra="forbid")

    source_document_ids: list[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="IDs from upload or admin catalog; max 10 per batch",
    )
    assessment_mode: AssessmentMode = AssessmentMode.WITH_QUIZ
    ingestion_instructions: str | None = None
    cards_per_module: int | None = None
    quizzes_per_module: int | None = None
    override_duplicates: list[bool] | None = Field(
        None,
        description=(
            "Optional booleans aligned to source_document_ids order; when true, "
            "re-ingest an already-ingested document by cloning a new source_document row"
        ),
    )


class IngestStartAcceptedSource(BaseModel):
    """One source queued by ``POST /admin/ingest``."""

    source_document_id: uuid.UUID
    run_id: uuid.UUID
    title: str
    source_type: str
    stored_path: str


class IngestStartResponse(BaseModel):
    """Result of ``POST /admin/ingest`` when at least one source was queued."""

    status: Literal["batch_queued"] = "batch_queued"
    batch_id: uuid.UUID
    poll_url: str
    sources: list[IngestStartAcceptedSource] = Field(default_factory=list)
    skipped_duplicates: list[IngestDuplicateConflict] = Field(default_factory=list)


class IngestProgressNode(BaseModel):
    """One node in the batch ingest progress tree (only nodes that have progressed)."""

    key: str
    title: str
    description: str
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: dict[str, Any] | None = None
    activity: str | None = None
    candidate_id: uuid.UUID | None = None
    chunk_id: str | None = None
    proposed_title: str | None = None
    fusion: bool | None = None
    published_module_merge: dict[str, Any] | None = None
    input_summary: dict[str, Any] | None = None
    output_summary: dict[str, Any] | None = None
    children: list[IngestProgressNode] = Field(default_factory=list)


class IngestBatchSourceProgress(BaseModel):
    source_document_id: uuid.UUID
    run_id: uuid.UUID
    document_label: str = ""
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: dict[str, Any] | None = None
    nodes: list[IngestProgressNode] = Field(default_factory=list)


class IngestBatchFusionProgress(BaseModel):
    key: str = "fusion"
    title: str
    description: str
    run_id: uuid.UUID
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: dict[str, Any] | None = None
    source_document_ids: list[str] | None = None
    nodes: list[IngestProgressNode] = Field(default_factory=list)


class IngestBatchPollResponse(BaseModel):
    """Tree-shaped progress for ``GET /admin/ingest/batches/{batch_id}``."""

    batch_id: uuid.UUID
    status: str
    created_at: datetime | None = None
    completed_at: datetime | None = None
    error: dict[str, Any] | None = None
    sources: list[IngestBatchSourceProgress] = Field(default_factory=list)
    fusion: IngestBatchFusionProgress | None = None
    retry_url: str | None = Field(
        None,
        description=(
            "POST this URL with no body to retry every retryable failed stage in the batch; "
            "null when nothing is retryable"
        ),
    )


class IngestBatchRetryStageResult(BaseModel):
    """One stage outcome from ``POST /admin/ingest/batches/{batch_id}/retry``."""

    run_id: uuid.UUID
    stage: str
    status: str
    candidate_id: uuid.UUID | None = None
    chunk_id: str | None = None
    reason: str | None = None


class IngestBatchRetryResponse(BaseModel):
    """Result of ``POST /admin/ingest/batches/{batch_id}/retry``."""

    batch_id: uuid.UUID
    results: list[IngestBatchRetryStageResult] = Field(default_factory=list)
    poll_url: str


class IngestMergeOverrideResponse(BaseModel):
    """Result of ``POST /admin/ingest/modules/{module_id}/override-merge``."""

    primary_module_id: uuid.UUID
    secondary_module_id: uuid.UUID
    source_module_id: uuid.UUID
    secondary_lifecycle_status: str


IngestProgressNode.model_rebuild()
