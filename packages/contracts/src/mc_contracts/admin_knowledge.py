"""Admin knowledge API contracts — platform → admin dashboard."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class KnowledgeSplitSpec(BaseModel):
    """One page-range split requested by ``POST /admin/knowledge/upload``."""

    start_page: int = Field(..., ge=1, description="1-based inclusive start page")
    end_page: int = Field(..., ge=1, description="1-based inclusive end page")
    title: str = Field(..., min_length=1)
    thumbnail_storage_path: str | None = None


class KnowledgeUploadedSource(BaseModel):
    """One source_document created by ``POST /admin/knowledge/upload``."""

    source_document_id: uuid.UUID
    title: str
    stored_path: str
    thumbnail_storage_path: str | None = None
    start_page: int | None = None
    end_page: int | None = None


class KnowledgeUploadResponse(BaseModel):
    """Result of ``POST /admin/knowledge/upload``."""

    sources: list[KnowledgeUploadedSource] = Field(default_factory=list)
