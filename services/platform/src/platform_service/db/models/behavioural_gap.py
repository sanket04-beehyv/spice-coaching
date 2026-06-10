"""v3.3 behavioural_gap — registry of CHW behaviour patterns.

Per Data Model v3.3 §6.1. Seeded from Annex 1 of the 27 Feb 2026 requirements
PDF (drop System + Coaching rows) plus 2-3 BRAC clinical lead additions.
Pilot target 8-12 active gaps.

Detection rule lives in detection_rule_jsonb; consumed by the gap-detection
worker (W-8) and synced to device for SDK-side visit-time evaluation
(Architecture R9 hybrid trigger evaluation).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class BehaviouralGap(Base):
    __tablename__ = "behavioural_gap"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Stable code, e.g. incorrect_referral_destination. Globally unique;
    # deprecated codes cannot be reused.
    gap_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    # low | moderate | high
    severity_default: Mapped[str] = mapped_column(Text, nullable=False, default="moderate")
    # Telemetry-pattern definition consumed by gap-detection workers.
    detection_rule_jsonb: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # active | deprecated
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
