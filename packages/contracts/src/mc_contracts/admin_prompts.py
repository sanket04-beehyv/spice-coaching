"""Admin dashboard prompt template management API contracts."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PromptTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    template_id: str
    version: int
    variant_key: str | None = None
    generation_type: str
    system_prompt_template: str
    human_message_template: str
    required_variables: list[str]
    title: str | None = None
    description: str | None = None
    change_notes: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class PromptTemplateCatalogEntry(BaseModel):
    template_id: str
    variant_key: str | None = None
    active_version: int
    generation_type: str
    title: str | None = None
    description: str | None = None
    required_variables: list[str]
    updated_at: datetime


class PromptTemplateCreateVersionRequest(BaseModel):
    variant_key: str | None = None
    system_prompt_template: str
    human_message_template: str
    required_variables: list[str] = Field(default_factory=list)
    title: str | None = None
    description: str | None = None
    change_notes: str = Field(min_length=1)


class PromptTemplateActivateRequest(BaseModel):
    variant_key: str | None = None


class PromptTemplatePreviewRequest(BaseModel):
    variant_key: str | None = None
    version: int | None = None
    variables: dict[str, str] = Field(default_factory=dict)


class PromptTemplatePreviewResponse(BaseModel):
    template_id: str
    template_version: int
    prompt_template_id: UUID
    resolved_system_prompt: str
    resolved_human_message: str


class PromptTemplateVariablesResponse(BaseModel):
    template_id: str
    variant_key: str | None = None
    active_version: int
    required_variables: list[str]
