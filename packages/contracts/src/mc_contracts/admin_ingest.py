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


class IngestCardinalityTargets(BaseModel):
    """Optional fixed card/quiz counts set at ingest time (batch-wide)."""

    cards_per_module: int | None = Field(
        None,
        description="Fixed number of cards per module; must be within deployment card bounds",
    )
    quizzes_per_module: int | None = Field(
        None,
        description="Fixed number of quiz questions per module; must be within deployment quiz bounds",
    )
