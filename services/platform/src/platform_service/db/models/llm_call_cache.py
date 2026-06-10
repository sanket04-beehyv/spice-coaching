"""v3.3 llm_call_cache — cached LLM responses for cheap retry.

Per Data Model v3.3 §4.3. Keys responses by hash of (prompt + input + model).
Used by pipeline orchestrator (W-7) to recover from transient failures and to
keep deterministic re-runs cheap during development.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class LlmCallCache(Base):
    __tablename__ = "llm_call_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Hash of (prompt + input payload + model). Unique so duplicate inputs
    # are deduplicated.
    input_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    response_jsonb: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # { "input_tokens": N, "output_tokens": M, "cost_usd_estimated": X }
    token_usage_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cached_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
