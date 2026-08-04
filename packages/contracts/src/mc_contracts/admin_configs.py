"""Admin dashboard configuration management API contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ConfigThresholdResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    key: str
    value_json: Any
    title: str | None = None
    description: str | None = None
    created_at: datetime
    updated_at: datetime


class ConfigThresholdCreateRequest(BaseModel):
    key: str
    value_json: Any
    title: str | None = None
    description: str | None = None


class ConfigThresholdUpdateRequest(BaseModel):
    value_json: Any
    title: str | None = None
    description: str | None = None
