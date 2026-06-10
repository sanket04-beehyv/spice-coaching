"""Admin ingest API contracts — platform → admin dashboard."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ExistingIngestedSourceSummary(BaseModel):
    """A source_document that already ingested the same content bytes."""

    source_document_id: uuid.UUID
    title: str
    original_filename: str | None = None
    ingested_at: datetime
    status: str


class IngestDuplicateConflict(BaseModel):
    """One uploaded file blocked because matching content is already ingested."""

    filename: str
    title: str
    content_sha256: str
    existing_source_documents: list[ExistingIngestedSourceSummary]


class FusionRequest(BaseModel):
    """Body for POST /admin/fusion. Accepts a list of source_document_id
    UUIDs that have completed Stage 2a (i.e. their per-source ingestion run
    succeeded with at least one candidate). Returns a fusion_run_id the
    caller can use to inspect the resulting fused modules."""

    source_document_ids: list[uuid.UUID] = Field(
        ...,
        min_length=2,
        description=(
            "≥2 source_document_id values to fuse. Each must already have "
            "a completed Stage 2a ingestion run with candidates persisted."
        ),
    )
