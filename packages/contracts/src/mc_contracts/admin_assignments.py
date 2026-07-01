"""Admin dashboard module assignments API contracts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from mc_contracts.localized import LocalizedString


class UserResponse(BaseModel):
    id: int
    name: str
    role: str
    district: str
    upazila: str | None = None
    parent_id: int | None = None


class AssignmentCreateRequest(BaseModel):
    module_id: UUID
    assignment_type: str = Field(..., description="individual | po_sk | geographical | group")
    user_ids: list[int] | None = None
    tenant_ids: list[int] | None = None
    upazilas: list[str] | None = None


class AssignmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    module_id: UUID
    module_title: LocalizedString | None = None
    assignment_type: str
    tenant_id: int | None = None
    user_id: int | None = None
    user: UserResponse | None = None
    upazila: str | None = None
    assigned_by: int
    assigned_at: datetime
    created_at: datetime
    updated_at: datetime
