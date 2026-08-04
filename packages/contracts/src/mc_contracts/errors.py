"""Shared error catalogue and RFC 7807 Problem Details DTOs.

Clients map ``code`` to user-facing copy. ``detail`` is technical/debug text.

Human-readable catalogue for clients: ``docs/error-codes.json`` (repo root).
When adding, removing, or renaming an ``ErrorCode``, update that file in the
same change; pre-commit enforces parity.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

ERROR_CATALOG_PATH = "docs/error-codes.json"


class ErrorCode(str, Enum):
    """Stable machine-readable error codes shared by platform and ai-runtime."""

    # Cross-cutting HTTP
    VALIDATION_ERROR = "validation_error"
    NOT_AUTHENTICATED = "not_authenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    BAD_REQUEST = "bad_request"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL_ERROR = "internal_error"
    SERVICE_UNAVAILABLE = "service_unavailable"
    BAD_GATEWAY = "bad_gateway"

    # Ingest / run state
    BATCH_NOT_FOUND = "batch_not_found"
    RUN_NOT_FOUND = "run_not_found"
    STEP_NOT_FOUND = "step_not_found"
    SOURCE_NOT_FOUND = "source_not_found"
    SOURCE_NOT_UPLOADED = "source_not_uploaded"
    UNKNOWN_STAGE = "unknown_stage"
    DUPLICATE_CONTENT = "duplicate_content"
    MERGE_OVERRIDE_NOT_PRIMARY = "merge_override_not_primary"
    MERGE_OVERRIDE_NOT_REVIEW_PENDING = "merge_override_not_review_pending"
    MERGE_OVERRIDE_SECONDARY_UNAVAILABLE = "merge_override_secondary_unavailable"
    MERGE_OVERRIDE_SOURCE_UNAVAILABLE = "merge_override_source_unavailable"
    INVALID_INGESTION_INSTRUCTIONS = "invalid_ingestion_instructions"
    INVALID_CARDINALITY_TARGETS = "invalid_cardinality_targets"
    CONCURRENT_RUN = "concurrent_run"
    STEP_NOT_FAILED = "step_not_failed"
    RETRY_NOT_ALLOWED = "retry_not_allowed"
    CANDIDATE_REQUIRED = "candidate_required"
    CHUNK_REQUIRED = "chunk_required"
    CHUNK_ID_INVALID = "chunk_id_invalid"
    MODULE_ID_MISSING = "module_id_missing"
    FUSION_SOURCES_MISSING = "fusion_sources_missing"

    # Modules / admin
    MODULE_NOT_FOUND = "module_not_found"
    MODULE_VERSION_CONFLICT = "module_version_conflict"
    OBJECT_NOT_FOUND = "object_not_found"
    FILENAME_REQUIRED = "filename_required"
    PROMPT_NOT_FOUND = "prompt_not_found"
    TRIGGER_BINDING_NOT_FOUND = "trigger_binding_not_found"
    ASSIGNMENT_NOT_FOUND = "assignment_not_found"
    ASSIGNMENT_VALIDATION_ERROR = "assignment_validation_error"
    INVALID_QUERY = "invalid_query"
    CONFIG_NOT_FOUND = "config_not_found"
    MODULE_LIFECYCLE_ERROR = "module_lifecycle_error"
    MODULE_FAMILY_NOT_ASSIGNABLE = "module_family_not_assignable"
    GAP_LINK_ERROR = "gap_link_error"

    # Auth / identity
    CHW_ID_REQUIRED = "chw_id_required"
    USER_ID_MISSING = "user_id_missing"
    TENANT_MISMATCH = "tenant_mismatch"

    # AI / integrations
    AI_RUNTIME_UNREACHABLE = "ai_runtime_unreachable"
    AI_RUNTIME_ERROR = "ai_runtime_error"
    EMPTY_EMBEDDING = "empty_embedding"
    EMPTY_TRANSCRIPT = "empty_transcript"
    UNSUPPORTED_MIME_TYPE = "unsupported_mime_type"
    INVALID_BASE64 = "invalid_base64"
    EMPTY_MEDIA_PAYLOAD = "empty_media_payload"
    EMBEDDING_DIMENSION_MISMATCH = "embedding_dimension_mismatch"
    OBJECT_STORAGE_ERROR = "object_storage_error"
    ANALYTICS_UNAVAILABLE = "analytics_unavailable"
    COACHING_RAG_ERROR = "coaching_rag_error"
    INTERNAL_TOKEN_INVALID = "internal_token_invalid"

    # Async / worker failures
    PIPELINE_CRASHED = "pipeline_crashed"
    STAGE_FAILED = "stage_failed"
    EXTRACT_FAILED = "extract_failed"
    IDENTIFY_FAILED = "identify_failed"
    IDENTIFY_NO_CANDIDATES = "identify_no_candidates"
    DRAFT_FAILED = "draft_failed"
    FUSION_FAILED = "fusion_failed"
    THUMBNAIL_FAILED = "thumbnail_failed"
    EMBEDDING_FAILED = "embedding_failed"
    SEARCH_METADATA_FAILED = "search_metadata_failed"
    CARD_SEARCH_METADATA_FAILED = "card_search_metadata_failed"
    TRIGGER_BINDING_FAILED = "trigger_binding_failed"
    GAP_CLASSIFICATION_FAILED = "gap_classification_failed"
    QUIZ_GENERATION_FAILED = "quiz_generation_failed"
    ENQUEUE_FAILED = "enqueue_failed"
    MODULE_HAS_NO_TEXT = "module_has_no_text"
    GENERATION_FAILED = "generation_failed"


class ValidationErrorItem(BaseModel):
    """One field-level validation failure (FastAPI/Pydantic shape)."""

    loc: list[str | int] = Field(default_factory=list)
    msg: str
    type: str = "value_error"


class ProblemDetails(BaseModel):
    """RFC 7807 Problem Details with MicroCoaching ``code`` extension."""

    type: str
    title: str
    status: int
    detail: str
    instance: str | None = None
    code: str
    errors: list[ValidationErrorItem] | None = None
    checks: dict[str, str] | None = None

    def model_dump_response(self) -> dict[str, Any]:
        """Omit null optional extensions for a compact JSON body."""
        data = self.model_dump(mode="json")
        if data.get("errors") is None:
            data.pop("errors", None)
        if data.get("checks") is None:
            data.pop("checks", None)
        if data.get("instance") is None:
            data.pop("instance", None)
        return data


def error_type_ref(code: str) -> str:
    """Relative catalog reference for Problem Details ``type``."""
    return f"{ERROR_CATALOG_PATH}#{code}"


def title_from_code(code: str) -> str:
    """Human-readable title derived from a snake_case error code."""
    return code.replace("_", " ").strip().title() or "Error"
