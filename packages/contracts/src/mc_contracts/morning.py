from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class MorningModuleSuggestionItem(BaseModel):
    module_id: UUID
    module_family_id: UUID
    source: Literal["gap", "quiz", "fallback"]
    behavioural_gap_id: UUID | None = None
    quiz_id: UUID | None = None


class MorningCardsResponse(BaseModel):
    items: list[MorningModuleSuggestionItem]
    total_points: int = 0
