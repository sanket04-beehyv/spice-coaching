"""Admin module demand summary API contracts."""

from __future__ import annotations

import enum
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ModuleDemandAction(str, enum.Enum):
    """Per-row click action for the admin demand summary UI."""

    ASSIGN = "assign"
    OPEN_DRAFT = "open_draft"
    CREATE = "create"


class ModuleDemandSource(str, enum.Enum):
    """Where a requestor signal came from."""

    FORM = "form"
    CHATBOT = "chatbot"


class ModuleDemandItem(BaseModel):
    display_name: str
    request_count: int
    module_id: UUID | None = None
    lifecycle_status: str | None = None
    domain: str | None = None
    action: ModuleDemandAction
    domain_filter: str | None = None


class ModuleDemandSummaryResponse(BaseModel):
    top_k: int
    generated_at: datetime
    llm_summary: str
    available: list[ModuleDemandItem]
    unavailable: list[ModuleDemandItem]


class ModuleDemandRequestor(BaseModel):
    chw_id: int
    chw_name: str | None = None
    source: ModuleDemandSource
    requested_at: datetime
    already_assigned: bool
    request_id: UUID | None = None  # set for form requests; null for chatbot-derived demand


class ModuleDemandRequestorsResponse(BaseModel):
    module_id: UUID
    module_title: str
    requestors: list[ModuleDemandRequestor]


class ModuleDemandAssignRequest(BaseModel):
    user_ids: list[int] = Field(..., min_length=1)


class ModuleDemandAssignResponse(BaseModel):
    assigned_count: int
    assignment_ids: list[str]
