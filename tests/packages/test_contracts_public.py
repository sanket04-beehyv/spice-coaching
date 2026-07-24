"""Validation tests for public API contract DTOs."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from mc_contracts.enums import CoachingEventType, EventFamily
from mc_contracts.localized import LocaleConfig
from mc_contracts.sync import ConfigSyncBundle, ModulesSyncBundle
from mc_contracts.telemetry import TelemetryBatch, TelemetryEvent
from pydantic import ValidationError


def test_telemetry_batch_rejects_too_many_events() -> None:
    with pytest.raises(ValidationError):
        TelemetryBatch(
            sdk_version="1.0",
            chw_id=1,
            tenant_id=uuid4(),
            events=[
                TelemetryEvent(
                    id=f"evt-{i}",
                    event_family=EventFamily.COACHING,
                    event_type="module_viewed",
                    event_date=date.today(),
                    timestamp_local=1,
                )
                for i in range(501)
            ],
        )


def test_telemetry_batch_accepts_valid_event() -> None:
    batch = TelemetryBatch(
        sdk_version="1.0",
        chw_id=1,
        tenant_id=uuid4(),
        events=[
            TelemetryEvent(
                id="evt-1",
                event_family=EventFamily.COACHING,
                event_type="module_viewed",
                event_date=date.today(),
                timestamp_local=1,
            )
        ],
    )
    assert batch.events[0].id == "evt-1"


def test_telemetry_batch_accepts_module_requested() -> None:
    batch = TelemetryBatch(
        sdk_version="1.0",
        chw_id=1,
        tenant_id=uuid4(),
        events=[
            TelemetryEvent(
                id="evt-req-1",
                event_family=EventFamily.COACHING,
                event_type=CoachingEventType.MODULE_REQUESTED,
                module_id=uuid4(),
                payload_json={"reason": "Need refresher"},
                event_date=date.today(),
                timestamp_local=1,
            )
        ],
    )
    assert batch.events[0].event_type == CoachingEventType.MODULE_REQUESTED
    assert batch.events[0].event_type.value == "module_requested"


def test_modules_sync_bundle_requires_lists() -> None:
    bundle = ModulesSyncBundle(
        modules=[],
        module_families=[],
        server_time_utc=datetime.now(UTC).isoformat(),
    )
    assert bundle.modules == []
    assert bundle.assigned_module_ids == []
    assert bundle.requested_modules == []


def test_config_sync_bundle_accepts_threshold_map() -> None:
    bundle = ConfigSyncBundle(
        thresholds={"gap_escalation_days": 7},
        locales=LocaleConfig(primary="bn", supported=["bn"]),
        server_time_utc="2026-01-01T00:00:00Z",
    )
    assert bundle.thresholds["gap_escalation_days"] == 7
    assert bundle.locales.primary == "bn"
    assert bundle.locales.supported == ["bn"]
