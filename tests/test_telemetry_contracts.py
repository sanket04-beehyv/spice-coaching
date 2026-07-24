from __future__ import annotations

from datetime import date

from mc_contracts.enums import DigitalEventType, EventFamily
from mc_contracts.telemetry import TelemetryEvent


class TestTelemetryEventDigitalFeedbackTypes:
    def test_accepts_positive_feedback_event_type(self) -> None:
        e = TelemetryEvent(
            id="evt-1",
            event_family=EventFamily.DIGITAL,
            event_type=DigitalEventType.CHAT_FEEDBACK_POSITIVE,
            event_date=date(2026, 1, 1),
            timestamp_local=1,
        )
        assert e.event_family is EventFamily.DIGITAL
        assert e.event_type == DigitalEventType.CHAT_FEEDBACK_POSITIVE

    def test_accepts_negative_feedback_event_type(self) -> None:
        e = TelemetryEvent(
            id="evt-2",
            event_family=EventFamily.DIGITAL,
            event_type=DigitalEventType.CHAT_FEEDBACK_NEGATIVE,
            event_date=date(2026, 1, 1),
            timestamp_local=2,
        )
        assert e.event_family is EventFamily.DIGITAL
        assert e.event_type == DigitalEventType.CHAT_FEEDBACK_NEGATIVE

    def test_round_trip_json(self) -> None:
        e = TelemetryEvent(
            id="evt-3",
            event_family=EventFamily.DIGITAL,
            event_type=DigitalEventType.CHAT_FEEDBACK_POSITIVE,
            payload_json={"message_id": "m-1", "reason": "helpful"},
            event_date=date(2026, 1, 1),
            timestamp_local=123,
        )
        rebuilt = TelemetryEvent.model_validate_json(e.model_dump_json())
        assert rebuilt.event_family is EventFamily.DIGITAL
        assert rebuilt.event_type == DigitalEventType.CHAT_FEEDBACK_POSITIVE
        assert rebuilt.payload_json["message_id"] == "m-1"
