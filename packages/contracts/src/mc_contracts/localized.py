"""Locale-keyed content types shared across platform API contracts."""

from __future__ import annotations

from typing import TypeAlias

from pydantic import BaseModel, Field

# Locale code (ISO 639-1 / short BCP-47) → localized text.
LocalizedString: TypeAlias = dict[str, str]

# Locale code → list of option strings (quiz).
LocalizedOptions: TypeAlias = dict[str, list]


class LocaleConfig(BaseModel):
    """Deployment language configuration pushed to clients via sync config."""

    primary: str = Field(description="CHW-facing locale for this deployment")
    supported: list[str] = Field(
        default_factory=list,
        description="Locale codes present in synced content for this deployment",
    )
