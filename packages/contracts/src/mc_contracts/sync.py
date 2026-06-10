from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class ConfigSyncBundle(BaseModel):
    """Configurable thresholds pushed to device for offline use."""

    thresholds: dict  # key → value_json
    server_time_utc: str


class ModuleQuizQuestionPayload(BaseModel):
    id: UUID
    question_order: int | None
    question_bn: str
    question_en: str | None
    case_setup_bn: str | None
    case_setup_en: str | None
    options_bn: list[Any]
    options_en: list[Any] | None
    correct_indices: list[int]
    explanation_bn: str | None
    explanation_en: str | None
    difficulty: str


class SourceDocumentSyncPayload(BaseModel):
    source_document_id: UUID
    title: str
    source_type: str
    primary_language: str
    content_domain: str
    assessment_mode: str
    authority_label: str
    version_label: str | None = None
    publication_date: date | None = None
    original_filename: str | None = None
    has_thumbnail: bool = False


class ModuleSyncPayload(BaseModel):
    id: UUID
    module_family_id: UUID
    version: int
    title_bn: str
    title_en: str | None
    description_bn: str | None
    description_en: str | None
    domain: str
    sub_domain: str | None
    module_type: str
    tenant_id: UUID | None
    estimated_minutes: int
    difficulty_level: str
    pass_threshold_override: float | None
    clinically_reviewed: bool
    published_at: datetime | None
    updated_at: datetime
    source_documents: list[SourceDocumentSyncPayload] = Field(default_factory=list)
    has_thumbnail: bool = False
    cards: list[dict[str, Any]]
    quiz: list[ModuleQuizQuestionPayload]


class ModuleFamilySyncPayload(BaseModel):
    id: UUID
    module_code: str
    created_at: datetime
    created_by: UUID | None
    current_published_module_id: UUID | None


class ModulesSyncBundle(BaseModel):
    modules: list[ModuleSyncPayload]
    module_families: list[ModuleFamilySyncPayload]
    server_time_utc: str


class TriggerDefinitionSyncPayload(BaseModel):
    id: UUID
    trigger_kind: str
    trigger_code: str
    description: str | None
    predicate_jsonb: dict[str, Any]
    predicate_schema_version: int
    status: str
    tenant_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ModuleTriggerBindingSyncPayload(BaseModel):
    id: UUID
    trigger_definition_id: UUID
    module_family_id: UUID
    relationship: str
    priority_weight: int
    notes: str | None


class TriggersSyncBundle(BaseModel):
    triggers: list[TriggerDefinitionSyncPayload]
    bindings: list[ModuleTriggerBindingSyncPayload]
    server_time_utc: str


class BehaviouralGapSyncPayload(BaseModel):
    id: UUID
    gap_code: str
    description: str
    domain: str
    severity_default: str
    detection_rule_jsonb: dict[str, Any]
    updated_at: datetime


class CHWBehaviouralGapStateSyncPayload(BaseModel):
    chw_id: int
    behavioural_gap_id: UUID
    tenant_id: UUID | None
    severity_current: str
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    last_reinforced_at: datetime | None
    occurrence_count: int
    failed_attempts_count: int
    last_failed_attempt_at: datetime | None
    escalated_to_supervisor: bool
    status: str
    updated_at: datetime | None


class CHWModuleCompletionSyncPayload(BaseModel):
    chw_id: int
    module_family_id: UUID
    latest_completed_module_id: UUID | None
    latest_attempt_module_id: UUID | None
    completed_at: datetime | None
    latest_attempt_at: datetime | None
    latest_quiz_score: float | None
    latest_attempt_passed: bool
    attempts_since_last_pass: int
    reinforcement_due_at: datetime | None
    tenant_id: UUID | None


class CHWModulePartialCompletionSyncPayload(BaseModel):
    chw_id: int
    module_id: UUID
    module_family_id: UUID
    incomplete_quiz_ids: list[UUID]
    tenant_id: UUID | None = None


class GapsSyncBundle(BaseModel):
    behavioural_gaps: list[BehaviouralGapSyncPayload]
    chw_behavioural_gap_states: list[CHWBehaviouralGapStateSyncPayload]
    chw_module_completions: list[CHWModuleCompletionSyncPayload]
    chw_module_partial_completions: list[CHWModulePartialCompletionSyncPayload] = Field(default_factory=list)
    server_time_utc: str
    total_points: int = 0


class SourceDocumentsPresignRequest(BaseModel):
    source_document_ids: list[UUID] = Field(max_length=50)


class SourceDocumentPresignedUrlPayload(BaseModel):
    source_document_id: UUID
    storage_path: str
    presigned_url: str
    expires_seconds: int


class SourceDocumentsPresignResponse(BaseModel):
    urls: list[SourceDocumentPresignedUrlPayload]
    missing_ids: list[UUID]
    server_time_utc: str


class ModuleThumbnailsPresignRequest(BaseModel):
    module_ids: list[UUID] = Field(max_length=50)


class ModuleThumbnailPresignedUrlPayload(BaseModel):
    module_id: UUID
    storage_path: str
    presigned_url: str
    expires_seconds: int


class ModuleThumbnailsPresignResponse(BaseModel):
    urls: list[ModuleThumbnailPresignedUrlPayload]
    missing_ids: list[UUID]
    server_time_utc: str


class SourceDocumentThumbnailsPresignRequest(BaseModel):
    source_document_ids: list[UUID] = Field(max_length=50)


class SourceDocumentThumbnailPresignedUrlPayload(BaseModel):
    source_document_id: UUID
    storage_path: str
    presigned_url: str
    expires_seconds: int


class SourceDocumentThumbnailsPresignResponse(BaseModel):
    urls: list[SourceDocumentThumbnailPresignedUrlPayload]
    missing_ids: list[UUID]
    server_time_utc: str
