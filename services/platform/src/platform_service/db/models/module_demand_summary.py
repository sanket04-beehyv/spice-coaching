"""Cached admin module-demand summary, refreshed by a daily Celery job.

The summary (form + chatbot demand, ranked buckets, LLM narrative) is expensive
to compute per request — it fans out to ClickHouse and ai-runtime. A daily job
precomputes one snapshot per tenant scope and the admin API reads it back.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class ModuleDemandSummary(Base):
    __tablename__ = "module_demand_summary"
    __table_args__ = (Index("ix_module_demand_summary_tenant", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Null tenant_id is the global (untenanted) scope.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    # Serialized ModuleDemandSummaryResponse (available/unavailable items + narrative).
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
