"""Dashboard view model contracts — platform → dashboard frontend."""

from __future__ import annotations

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
    """One ranked module by digital_help_used event volume.

    Ranked by concrete ``module_id`` (not family) so demand/usage is never
    collapsed across module versions.
    """

    module_id: UUID
    module_family_id: UUID | None = None
    query_count: int
    title: LocalizedString | None = None


class DigitalHelpModuleUsageResponse(BaseModel):
    period_days: int
    total_queries: int
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
