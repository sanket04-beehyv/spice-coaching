"""Dashboard view model contracts — platform → dashboard frontend."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from mc_contracts.localized import LocalizedString


class GapSummary(BaseModel):
    scenario_id: str
    wrong_count: int
    skip_count: int
    gap_active: bool


class CHWSkillSnapshot(BaseModel):
    chw_id: int
    digital_help_used: int
    cards_shown: int
    cards_accepted: int
    incorrect_referrals: int = 0
    quiz_correct_rate: float | None
    active_gaps: list[GapSummary] = Field(default_factory=list)


class SupervisorDashboardResponse(BaseModel):
    """Tier 1 dashboard — data we own, no SPICE enrichment needed."""

    chw_id: int
    period_days: int = 30
    chw_snapshot: CHWSkillSnapshot
    top_gap_scenarios: list[str] = Field(default_factory=list)
    validator_failure_rate: float | None = None
    fallback_rate: float | None = None


class DigitalHelpModuleUsageItem(BaseModel):
    """One ranked module by combined digital_help_used + module_requested volume.

    Ranked by concrete ``module_id`` (not family) so demand/usage is never
    collapsed across module versions. Free-text ``module_requested`` events
    without a ``module_id`` are excluded.
    """

    module_id: UUID
    module_family_id: UUID | None = None
    digital_help_count: int
    module_requested_count: int
    title: LocalizedString | None = None


class DigitalHelpModuleUsageResponse(BaseModel):
    from_date: date
    to_date: date
    total_digital_help: int
    total_module_requested: int
    total_modules: int = 0
    limit: int
    offset: int
    modules: list[DigitalHelpModuleUsageItem] = Field(default_factory=list)


class LLMQualityResponse(BaseModel):
    """AI runtime observability dashboard."""

    period_days: int = 7
    total_inferences: int
    digital_help_event_count: int = 0
    inference_online_count: int = 0
    inference_edge_count: int = 0
    inference_offline_count: int = 0
    validator_pass_count: int = 0
    validator_fail_count: int = 0
    fallback_used_count: int = 0
    avg_latency_ms: float | None = None
    validator_failure_rate: float | None = None
    fallback_rate: float | None = None
    error_rate: float | None = None
    avg_input_tokens: float | None = None
    avg_output_tokens: float | None = None


class TeamActivitySummary(BaseModel):
    total_users: int
    active_users: int
    non_active_users: int
    users_completed_module: int
    users_chatbot_engaged: int


class TeamMemberModuleActivity(BaseModel):
    module_id: UUID
    title: LocalizedString | None = None
    completed_in_range: bool
    completed_at: datetime | None = None


class TeamMemberChatbotModuleUsage(BaseModel):
    module_id: UUID
    title: LocalizedString | None = None
    query_count: int


class TeamMemberActivityDetail(BaseModel):
    user_id: int
    name: str
    is_active: bool
    is_chatbot_engaged: bool
    last_chat_at: datetime | None = None
    last_active_at: datetime | None = None
    has_completed_module_in_range: bool
    assigned_modules: list[TeamMemberModuleActivity] = Field(default_factory=list)
    chatbot_query_count: int = 0
    chatbot_unattributed_query_count: int = 0
    chatbot_modules: list[TeamMemberChatbotModuleUsage] = Field(default_factory=list)
    refreshers_generated: int = 0
    refreshers_completed: int = 0


class TeamActivityResponse(BaseModel):
    from_date: date
    to_date: date
    summary: TeamActivitySummary
    users: list[TeamMemberActivityDetail] = Field(default_factory=list)
    total_users: int
    total_pages: int
    limit: int
    offset: int
    server_time_utc: str


class TeamMemberQuestionItem(BaseModel):
    question: str
    occurrence_count: int
    last_asked_at: datetime


class TeamMemberQuestionsResponse(BaseModel):
    user_id: int
    from_date: date
    to_date: date
    questions: list[TeamMemberQuestionItem] = Field(default_factory=list)
    total_questions: int
    total_pages: int
    limit: int
    offset: int
    server_time_utc: str


class DigitalHelpModuleQuestionsResponse(BaseModel):
    """Paginated deduplicated chatbot questions for one module."""

    module_id: UUID
    title: LocalizedString | None = None
    from_date: date
    to_date: date
    questions: list[TeamMemberQuestionItem] = Field(default_factory=list)
    total_questions: int
    total_pages: int
    limit: int
    offset: int


class DigitalHelpModuleRequestsResponse(BaseModel):
    """Aggregate module_requested count for one concrete module_id."""

    module_id: UUID
    title: LocalizedString | None = None
    from_date: date
    to_date: date
    module_requested_count: int


class ModuleCreationSuggestionEvidenceItem(BaseModel):
    """One deduped chat question or free-text module request behind a suggestion."""

    source: str
    text: str
    occurrence_count: int
    last_seen_at: datetime | None = None
    sample_chw_id: int | None = None


class ModuleCreationSuggestionListItem(BaseModel):
    """One inferred module-creation suggestion for a UTC calendar day."""

    id: UUID
    suggestion_date: date
    suggestion_kind: str
    matched_module_id: UUID | None = None
    proposed_topic: str | None = None
    display_title: str
    rationale: str | None = None
    question_count: int
    request_count: int
    evidence_count: int
    rank: int
    computed_at: datetime


class ModuleCreationSuggestionListResponse(BaseModel):
    from_date: date
    to_date: date
    suggestions: list[ModuleCreationSuggestionListItem] = Field(default_factory=list)
    total_suggestions: int
    total_pages: int
    limit: int
    offset: int


class ModuleCreationSuggestionDetailResponse(BaseModel):
    suggestion: ModuleCreationSuggestionListItem
    questions: list[ModuleCreationSuggestionEvidenceItem] = Field(default_factory=list)
    requests: list[ModuleCreationSuggestionEvidenceItem] = Field(default_factory=list)


class DocumentUsageTopItem(BaseModel):
    """One ranked document by view volume."""

    document_id: UUID
    document_title: str | None = None
    view_count: int


class DocumentUsageDocumentRow(BaseModel):
    """Per-document usage row."""

    document_id: UUID
    document_title: str | None = None
    total_views: int
    unique_users: int
    last_viewed_at: datetime | None = None
    last_viewed_by_user_id: int | None = None
    last_viewed_by_user_name: str | None = None


class DocumentUsageEventRow(BaseModel):
    """One document-view event for drill-down."""

    event_id: str
    document_id: UUID
    document_title: str | None = None
    user_id: int
    user_name: str | None = None
    user_role: str | None = None
    upazila_id: str | None = None
    district: str | None = None
    viewed_at: datetime | None = None


class DocumentUsageResponse(BaseModel):
    """Combined document-view analytics for PO / AM / Admin dashboards.

    One response carries KPIs, the per-document table, and event drill-down
    under the same filters.
    """

    from_date: date
    to_date: date
    total_views: int
    unique_documents: int
    unique_users: int
    top_documents: list[DocumentUsageTopItem] = Field(default_factory=list)
    total_document_rows: int = 0
    documents: list[DocumentUsageDocumentRow] = Field(default_factory=list)
    total_events: int = 0
    events: list[DocumentUsageEventRow] = Field(default_factory=list)
    documents_limit: int
    documents_offset: int
    events_limit: int
    events_offset: int
