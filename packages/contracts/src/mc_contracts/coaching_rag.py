"""Coaching RAG API contracts — platform → Android / CHW clients."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from mc_contracts.enums import SourceDocumentType
from mc_contracts.localized import LocalizedString


class CoachingRagRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=4000)
    response_language: str = Field(
        "",
        description=(
            "Preferred language for the `answer` field. When empty, the deployment primary locale is used."
        ),
    )


class RetrievedModuleHit(BaseModel):
    module_id: UUID
    title: LocalizedString
    domain: str
    cosine_distance: float = Field(..., description="pgvector cosine distance; lower is more similar")


SourceTypeLiteral = Literal[
    SourceDocumentType.PDF.value,
    SourceDocumentType.PPTX.value,
    SourceDocumentType.DOCX.value,
    SourceDocumentType.AUDIO.value,
    SourceDocumentType.VIDEO.value,
]


class SourcePageRef(BaseModel):
    """One page-level reference inside a source document.

    ``start_ms`` / ``end_ms`` are populated only for AV chunk pages
    (NULL for PDF/DOCX/PPTX), so the UI can render either a page jump
    or a timecode seek without branching on ``source_type``.
    """

    page_number: int
    start_ms: int | None = None
    end_ms: int | None = None


class SourceAttribution(BaseModel):
    source_document_id: UUID
    title: str
    source_type: SourceTypeLiteral = Field(
        description="Mirrors source_document.source_type: pdf | pptx | docx | audio | video.",
    )
    storage_path: str
    object_name: str | None = Field(
        default=None,
        description="MinIO object key when storage_path is bucket/key; null for legacy filesystem paths.",
    )
    original_filename: str | None = None
    content_sha256: str | None = None
    page_numbers: list[int] = Field(
        default_factory=list,
        description="Distinct page_number values from cited card blocks (PDF/DOCX/PPTX).",
    )
    source_pages: list[SourcePageRef] = Field(
        default_factory=list,
        description="Page-level refs with optional AV timecodes (start_ms/end_ms).",
    )
    presigned_url: str | None = None
    presigned_expires_seconds: int | None = None
    linked_module_ids: list[UUID] = Field(
        default_factory=list,
        description="Published modules from retrieval that cite this source_document.",
    )


class CoachingRagResponse(BaseModel):
    answer: str
    retrieved_modules: list[RetrievedModuleHit]
    source_documents: list[SourceAttribution]
    model: str
    cited_module_ids: list[UUID] = Field(default_factory=list)
    suggested_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Follow-up questions answerable from retrieved module content; "
            "language matches request response_language."
        ),
    )
