"""Chat feedback summary snapshot contracts (weekly Celery job output)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ChatFeedbackEventCounts(BaseModel):
    positive: int = 0
    positive_online: int = 0
    positive_offline: int = 0
    negative_online: int = 0
    negative_offline: int = 0
    total: int = 0


class ChatFeedbackEventSample(BaseModel):
    event_id: str
    event_type: Literal["chat_feedback_positive", "chat_feedback_negative"]
    inference_mode: str | None = None
    question: str | None = None
    feedback: str | None = None
    answer_excerpt: str | None = None
    module_id: UUID | None = None
    occurred_at: datetime


class ChatFeedbackSummaryResponse(BaseModel):
    generated_at: datetime
    period_start: datetime | None = None
    period_end: datetime
    event_counts: ChatFeedbackEventCounts
    llm_summary: str
    positive_online_themes: list[str] = Field(default_factory=list)
    positive_offline_themes: list[str] = Field(default_factory=list)
    negative_online_recommendations: list[str] = Field(default_factory=list)
    negative_offline_recommendations: list[str] = Field(default_factory=list)
    sampled_events: list[ChatFeedbackEventSample] = Field(default_factory=list)
