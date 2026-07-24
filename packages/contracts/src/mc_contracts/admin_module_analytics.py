"""Admin module lifecycle and performance analytics contracts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ModuleLifecycleActionRequest(BaseModel):
    actor_id: UUID | None = None
    reason: str | None = None


class ModuleLifecycleStatePayload(BaseModel):
    module_id: UUID
    module_family_id: UUID
    lifecycle_status: str
    first_activated_at: datetime | None
    last_deactivated_at: datetime | None
    last_reactivated_at: datetime | None


class ModuleLifecycleEventPayload(BaseModel):
    id: UUID
    module_id: UUID
    event_type: str
    occurred_at: datetime
    actor_id: UUID | None
    reason: str | None


class ModulePerformanceSummary(BaseModel):
    module_family_id: UUID
    module_id: UUID | None
    module_code: str
    title_bn: str | None
    title_en: str | None
    lifecycle_status: str = Field(description="draft | published | retired | deactivated")
    family_created_at: datetime
    first_activated_at: datetime | None
    last_deactivated_at: datetime | None
    last_reactivated_at: datetime | None
    unique_chws_attempted: int
    unique_chws_completed: int
    total_attempts_in_range: int
