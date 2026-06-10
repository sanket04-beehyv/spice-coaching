"""Telemetry event schemas — SDK → platform → ClickHouse.

Notes:
  - Batch metadata (`sdk_version`, `chw_id`, `tenant_id`) is carried on `TelemetryBatch`
    and should not be duplicated on each event.
  - Enum-typed fields are **backward compatible**: we accept either enum values
    or raw strings to avoid breaking older SDKs.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from mc_contracts.enums import (
    CardType,
    ClinicalDomain,
    CoachingEventType,
    DigitalEventType,
    EventFamily,
    InferenceMode,
    Outcome,
    TriggerType,
    ValidatorStatus,
)


class TelemetryEvent(BaseModel):
    """One event row — mirrors coaching_events ClickHouse table structure."""

    id: str
    event_schema_version: int = 1

    session_id: str | None = None
    patient_visit_id: str | None = None
    patient_track_id: str | None = None
    patient_id_hash: str | None = None

    village_id: str | None = None
    upazila_id: str | None = None

    event_family: EventFamily
    event_type: CoachingEventType | DigitalEventType | str  # backward compatible

    module_family_id: UUID | None = None
    module_id: UUID | None = None
    card_family_id: UUID | None = None
    # module_quiz_question.id for the question being answered (MODULE_QUIZ_ATTEMPTED).
    quiz_id: UUID | None = None

    clinical_domain: ClinicalDomain | str | None = None
    card_type: CardType | str | None = None
    trigger_type: TriggerType | str | None = None
    inference_mode: InferenceMode | str | None = None  # online | edge | cached
    outcome: Outcome | str | None = None

    validator_status: ValidatorStatus | str | None = None  # pass | fail | fallback | warn
    fallback_used: bool | None = None
    network_state: str | None = None
    payload_json: dict[str, Any] = Field(default_factory=dict)

    event_date: date
    timestamp_utc: int | None = None
    timestamp_local: int

    # ── v3.3 module-pipeline fields (W-10) ─────────────────────────────
    # Set by the SDK on MODULE_DELIVERED / MODULE_CARD_VIEWED /
    # MODULE_QUIZ_ATTEMPTED. Optional on every other
    # event_type so older SDK versions and legacy scenario events keep
    # validating unchanged.
    module_version: int | None = None
    quiz_score_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Required on MODULE_QUIZ_ATTEMPTED. Range 0.0–1.0 (not 0–100).",
    )


# Per-batch event cap. Pilot devices send small batches (~tens). 500 is
# generous; anything larger smells like an SDK loop bug or a malicious
# payload. Pydantic enforces this at request-parse time.
_MAX_EVENTS_PER_BATCH = 500


class TelemetryBatch(BaseModel):
    """Batch payload from SDK — one sync push."""

    events: list[TelemetryEvent] = Field(..., max_length=_MAX_EVENTS_PER_BATCH)
    sdk_version: str
    chw_id: int
    tenant_id: UUID | None = None


class TelemetryAckResponse(BaseModel):
    """Acknowledgement response for a telemetry batch ingest request.

    `accepted`, `rejected`, and `duplicates` (W-10) are **event IDs** from the
    submitted batch, not counts. `buffered` (W-10) lists IDs that were stored
    in the retry queue because the analytics sink was unreachable — they
    will be flushed by the retry worker; the SDK should treat them as
    successfully ingested. `errors` contains human-readable reasons for
    rejections.
    """

    accepted: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    buffered: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
