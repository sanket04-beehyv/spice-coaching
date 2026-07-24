"""Admin dashboard API contracts — platform → admin dashboard."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from mc_contracts.localized import LocalizedOptions, LocalizedString


class ModuleSummary(BaseModel):
    """List-row response. Keeps the payload light; full content via GET /modules/:id."""

    id: UUID
    module_family_id: UUID
    version: int
    title: LocalizedString
    description: LocalizedString | None = None
    domain: str
    module_type: str
    lifecycle_status: str
    clinically_reviewed: bool
    has_visibility_window: bool
    card_count: int
    quiz_count: int
    estimated_minutes: int
    published_at: datetime | None
    created_at: datetime
    first_activated_at: datetime | None = None
    last_deactivated_at: datetime | None = None
    last_reactivated_at: datetime | None = None
    # Quality flags written by Stage 2 / Stage 2-draft (e.g.
    # `insufficient_source_filter`, drafter `insufficient_reason`). Surfaced
    # so the dashboard can build a "needs attention" view; presence of any
    # flag does NOT block publish.
    quality_flags: dict[str, Any] | None
    # LLM-generated bilingual keywords, search phrases, and topic tags for retrieval.
    search_metadata: dict[str, Any] | None = None
    chatbot_faqs_only: bool = False
    thumbnail_storage_path: str | None = None
    thumbnail_presigned_url: str | None = None
    thumbnail_presigned_expires_seconds: int | None = None
    # Per-module source document linkage for dashboard document filters.
    source_document_ids: list[str] | None = None


class ModuleListResponse(BaseModel):
    """Paginated admin module list envelope for ``GET /admin/modules``."""

    modules: list[ModuleSummary]
    total_modules: int
    total_pages: int
    limit: int
    offset: int


class ModuleSourceDocumentRef(BaseModel):
    """Linked source document with optional MinIO presigned GET URL."""

    source_document_id: UUID
    presigned_url: str | None = None
    presigned_expires_seconds: int | None = None
    thumbnail_storage_path: str | None = None
    thumbnail_presigned_url: str | None = None
    thumbnail_presigned_expires_seconds: int | None = None


class CardSourcePageRef(BaseModel):
    """One source page cited by a module card (via ``source_block_ids``)."""

    source_document_id: UUID
    page_number: int
    start_ms: int | None = Field(
        default=None,
        description="AV chunk start time in milliseconds; null for PDF/DOCX/PPTX pages.",
    )
    end_ms: int | None = Field(
        default=None,
        description="AV chunk end time in milliseconds; null for PDF/DOCX/PPTX pages.",
    )
    presigned_url: str | None = Field(
        default=None,
        description="Presigned GET URL for the source document with ``#page=N`` for PDF deep-linking.",
    )
    presigned_expires_seconds: int | None = None


class QuizQuestionPayload(BaseModel):
    id: UUID
    question_order: int | None
    question: LocalizedString
    case_setup: LocalizedString | None = None
    options: LocalizedOptions
    correct_indices: list[int]
    explanation: LocalizedString | None = None
    difficulty: str


class ModuleDetail(ModuleSummary):
    """Full module: shell + cards + quiz + attachment refs (no presigned URLs on file refs).

    Each card dict includes ``card_family_id``, may include ``source_block_ids`` (when
    pipeline-drafted) and a server-enriched ``source_pages`` list (``CardSourcePageRef`` shape).
    """

    cards: list[dict[str, Any]]
    attachments: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Module-level attachments from module_json; presign via GET /admin/files/presigned-url on demand",
    )
    quiz: list[QuizQuestionPayload]
    sub_domain: str | None
    estimated_minutes: int
    difficulty_level: str
    pass_threshold_override: float | None
    visibility_window_lower: datetime | None
    visibility_window_upper: datetime | None
    source_documents: list[ModuleSourceDocumentRef] = Field(default_factory=list)
    primary_gap_id: UUID | None = None
    behavioural_gap_ids: list[UUID] = Field(default_factory=list)


class QuizQuestionEditRequest(BaseModel):
    id: str | None = None
    question_order: int | None = None
    question: LocalizedString | None = None
    case_setup: LocalizedString | None = None
    options: LocalizedOptions
    correct_indices: list[int]
    explanation: LocalizedString | None = None
    difficulty: str = "moderate"


class ModuleEditRequest(BaseModel):
    """Edit a module tip; creates a new draft version unless the body is a complete
    content snapshot that matches the current tip (idempotent no-op).

    A complete snapshot requires ``title``, ``description``, ``module_json``,
    ``thumbnail_storage_path``, and quiz either as top-level ``quiz`` or nested under
    ``module_json.quiz``. Gap ids, ``editor_id``, and ``chatbot_faqs_only`` are ignored
    for equality. Omitted content fields always version-bump.
    """

    expected_version: int = Field(
        ...,
        ge=1,
        description=(
            "Version of the module row being edited; must match the server tip "
            "or the request fails with 409 module_version_conflict"
        ),
    )
    title: LocalizedString | None = None
    description: LocalizedString | None = None
    module_json: dict[str, Any] | None = None
    editor_id: UUID | None = None
    quiz: list[QuizQuestionEditRequest] | None = None
    behavioural_gap_ids: list[UUID] | None = Field(
        default=None,
        description="When set, replaces all gap links on the new module version",
    )
    primary_gap_id: UUID | None = Field(
        default=None,
        description="Primary gap for quiz state; must be in behavioural_gap_ids when both are set",
    )
    thumbnail_storage_path: str | None = Field(
        default=None,
        description=(
            "MinIO path to module preview image. Omit to copy forward on version bump; "
            "send null to clear; send a path to set or replace (upload via POST /admin/files)."
        ),
    )
    chatbot_faqs_only: bool | None = Field(
        default=None,
        description=(
            "When set, updates this module version flag. True restricts the module to "
            "chatbot knowledge retrieval only (no CHW training workflows)."
        ),
    )


class ModuleCreateRequest(BaseModel):
    title: LocalizedString
    description: LocalizedString | None = None
    domain: str = "clinical"
    sub_domain: str | None = None
    module_type: str = "refresher"
    estimated_minutes: int = 10
    difficulty_level: str = "moderate"
    module_json: dict[str, Any] | None = None
    quiz: list[QuizQuestionEditRequest] | None = None
    behavioural_gap_ids: list[UUID] | None = None
    primary_gap_id: UUID | None = None
    creator_id: UUID | None = None
    chatbot_faqs_only: bool = Field(
        default=False,
        description=(
            "When true, the module is for chatbot FAQ/RAG knowledge only and is excluded "
            "from CHW training, assignments, and refresher workflows."
        ),
    )


class ClinicalFlagRequest(BaseModel):
    clinically_reviewed: bool
    reviewer_id: UUID | None = None


class VisibilityWindowRequest(BaseModel):
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class SemanticSearchRequest(BaseModel):
    """Either a free-text query (server embeds it via ai-runtime) or a
    precomputed embedding vector. Callers without an embedding pipeline of
    their own should send `query` — the dashboard FE has no need to call
    /embed itself.
    """

    query: str | None = Field(
        None, description="free-text query; server embeds via ai-runtime before searching"
    )
    query_vector: list[float] | None = Field(
        None, description="precomputed embedding vector (escape hatch for batch tooling)"
    )
    limit: int = Field(10, ge=1, le=50)


class TriggerBindingPayload(BaseModel):
    id: UUID
    trigger_definition_id: UUID
    module_id: UUID
    # primary | secondary — bindings have no on/off flag; deactivation is
    # done via DELETE.
    relationship: str
    # Higher = preferred when multiple modules match the same trigger.
    priority_weight: int
    notes: str | None


class CreateBindingRequest(BaseModel):
    trigger_definition_id: UUID
    module_id: UUID
    relationship: str = "primary"
    priority_weight: int = 10
    notes: str | None = None


class UpdateBindingRequest(BaseModel):
    relationship: str | None = None
    priority_weight: int | None = None
    notes: str | None = None


class SourceDocumentSummary(BaseModel):
    """List-row response for admin source document catalog (ingest dropdowns)."""

    id: UUID
    title: str
    source_type: str
    status: str
    content_domain: str
    original_filename: str | None = None
    ingested_at: datetime


class SourceDocumentListResponse(BaseModel):
    """Paginated admin source document list envelope for ``GET /admin/source-documents``."""

    source_documents: list[SourceDocumentSummary]
    total_source_documents: int
    total_pages: int
    limit: int
    offset: int


class IngestionRunSummary(BaseModel):
    id: UUID
    source_document_id: UUID
    status: str
    started_at: datetime
    completed_at: datetime | None
    error: dict[str, Any] | None
    # Prefer original_filename when present; otherwise source_document.title.
    document_label: str = ""
    # Populated for succeeded runs only (else 0). Totals across modules
    # produced by this run's card_draft steps (distinct module_id).
    generated_module_count: int = 0
    generated_card_count: int = 0
    generated_quiz_count: int = 0


class IngestionRunListResponse(BaseModel):
    """Paginated admin ingestion-run list envelope for ``GET /admin/ingestion-runs``."""

    runs: list[IngestionRunSummary]
    total_runs: int
    total_pages: int
    limit: int
    offset: int


class IngestionRunCandidatePayload(BaseModel):
    candidate_id: UUID
    proposed_title: str
    domain: str | None = None
    behavioural_gap_code: str | None
    proposed_module_type: str | None
    estimated_card_count: int | None
    estimated_quiz_count: int | None
    quality_flags: dict[str, Any] | None = None
    ingestion_instruction_rationale: str | None = None


class PublishedModuleMergePoll(BaseModel):
    active: bool
    was_merge: bool
    merged_from_module_id: str | None = None


class IngestionRunStepPayload(BaseModel):
    id: UUID
    stage: str
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    input_summary: dict[str, Any] | None
    output_summary: dict[str, Any] | None
    error: dict[str, Any] | None
    activity: str | None = None
    fusion: bool | None = None
    published_module_merge: PublishedModuleMergePoll | None = None


class IngestionRunDetail(IngestionRunSummary):
    run_kind: str = "pipeline"
    steps: list[IngestionRunStepPayload]
    candidates: list[IngestionRunCandidatePayload] = Field(default_factory=list)
    current_activity: dict[str, Any] | None = None
    source_document_ids: list[str] | None = None
