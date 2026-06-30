"""v3.3 trigger_definition + module_trigger_binding — unified trigger schema.

Per Data Model v3.3 §6.2, §6.3. Replaces the gap-only `module_gap_mapping`
shape with a unified registry covering all three trigger classes:
- gap (telemetry-detected behavioural-gap pattern)
- workflow_event (SPICE workflow event)
- content_push (new module version published)

predicate_jsonb shape is per-kind; validated by application layer (W-8) using
JSON Schema validators per trigger_kind.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from platform_service.db.base import Base


class TriggerDefinition(Base):
    __tablename__ = "trigger_definition"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # gap | workflow_event | content_push
    trigger_kind: Mapped[str] = mapped_column(Text, nullable=False)
    # Stable identifier, e.g. gap:incorrect_referral_destination,
    # wf:assessment_submitted, push:module_version_published. Globally unique.
    trigger_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-kind structured predicate (Data Model §6.2 constraint table).
    predicate_jsonb: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Schema version for forward compatibility on predicate_jsonb shape.
    predicate_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # active | deprecated
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ModuleTriggerBinding(Base):
    __tablename__ = "module_trigger_binding"
    __table_args__ = (
        # At most one `primary` binding per (module, trigger_kind) — i.e.
        # a module version can have one primary gap-trigger AND one primary
        # workflow-event AND one primary content-push, but not two primary
        # gap-triggers. Enforced at application layer (would need a partial
        # index for SQL-level enforcement; deferred to repository validation).
        UniqueConstraint(
            "module_id",
            "trigger_definition_id",
            name="uq_module_trigger_binding_pair",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("module.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trigger_definition.id", ondelete="CASCADE"),
        nullable=False,
    )
    # primary | secondary
    relationship: Mapped[str] = mapped_column(Text, nullable=False, default="primary")
    # Higher = preferred when multiple modules respond to the same trigger.
    priority_weight: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
