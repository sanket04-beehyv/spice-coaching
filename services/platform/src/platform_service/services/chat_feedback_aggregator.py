"""Fetch chat feedback events from ClickHouse for weekly summary synthesis."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from mc_contracts.enums import DigitalEventType

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.config import Settings, get_settings

_POSITIVE_EVENT = DigitalEventType.CHAT_FEEDBACK_POSITIVE.value
_NEGATIVE_EVENT = DigitalEventType.CHAT_FEEDBACK_NEGATIVE.value
_ANSWER_EXCERPT_MAX_LEN = 500
_QUESTION_MAX_LEN = 300


@dataclass(frozen=True)
class FeedbackEvent:
    event_id: str
    tenant_id: UUID
    chw_id: int | None
    event_type: str
    inference_mode: str | None
    module_id: UUID | None
    question: str | None
    feedback: str
    answer_excerpt: str | None
    occurred_at: datetime

    @property
    def is_positive(self) -> bool:
        return self.event_type == _POSITIVE_EVENT

    @property
    def is_positive_online(self) -> bool:
        return self.is_positive and is_online_inference_mode(self.inference_mode)

    @property
    def is_positive_offline(self) -> bool:
        return self.is_positive and not self.is_positive_online

    @property
    def is_negative_online(self) -> bool:
        return self.event_type == _NEGATIVE_EVENT and is_online_inference_mode(self.inference_mode)

    @property
    def is_negative_offline(self) -> bool:
        return self.event_type == _NEGATIVE_EVENT and not self.is_negative_online


@dataclass(frozen=True)
class TenantFeedbackBatch:
    tenant_id: UUID
    events: list[FeedbackEvent]

    @property
    def positive_online_events(self) -> list[FeedbackEvent]:
        return [event for event in self.events if event.is_positive_online]

    @property
    def positive_offline_events(self) -> list[FeedbackEvent]:
        return [event for event in self.events if event.is_positive_offline]

    @property
    def negative_online_events(self) -> list[FeedbackEvent]:
        return [event for event in self.events if event.is_negative_online]

    @property
    def negative_offline_events(self) -> list[FeedbackEvent]:
        return [event for event in self.events if event.is_negative_offline]

    def event_counts(self) -> dict[str, int]:
        positive_online = len(self.positive_online_events)
        positive_offline = len(self.positive_offline_events)
        negative_online = len(self.negative_online_events)
        negative_offline = len(self.negative_offline_events)
        positive = positive_online + positive_offline
        return {
            "positive": positive,
            "positive_online": positive_online,
            "positive_offline": positive_offline,
            "negative_online": negative_online,
            "negative_offline": negative_offline,
            "total": positive + negative_online + negative_offline,
        }

    def sample_for_llm(
        self,
        *,
        max_positive_online: int,
        max_positive_offline: int,
        max_negative_online: int,
        max_negative_offline: int,
    ) -> list[FeedbackEvent]:
        sampled: list[FeedbackEvent] = []
        sampled.extend(self.positive_online_events[:max_positive_online])
        sampled.extend(self.positive_offline_events[:max_positive_offline])
        sampled.extend(self.negative_online_events[:max_negative_online])
        sampled.extend(self.negative_offline_events[:max_negative_offline])
        return sampled


def is_online_inference_mode(inference_mode: str | None) -> bool:
    return (inference_mode or "").casefold() == "online"


def _to_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return None


def _truncate(text: str | None, *, max_len: int) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if len(stripped) <= max_len:
        return stripped
    return stripped[: max_len - 3] + "..."


def _parse_payload(payload: dict[str, Any] | str | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    return payload


def _extract_feedback(payload: dict[str, Any] | str | None) -> str:
    parsed = _parse_payload(payload)
    if parsed is None:
        return ""
    feedback = parsed.get("feedback")
    if feedback is None:
        return ""
    return str(feedback).strip()


def _extract_question(payload: dict[str, Any] | str | None) -> str | None:
    parsed = _parse_payload(payload)
    if parsed is None:
        return None
    question = parsed.get("question")
    if question is None:
        return None
    return _truncate(str(question), max_len=_QUESTION_MAX_LEN)


def _extract_answer_excerpt(payload: dict[str, Any] | str | None) -> str | None:
    parsed = _parse_payload(payload)
    if parsed is None:
        return None

    response = parsed.get("response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            return None
    if not isinstance(response, dict):
        return None

    answer = response.get("answer")
    if answer is None:
        return None
    return _truncate(str(answer), max_len=_ANSWER_EXCERPT_MAX_LEN)


def _row_to_event(row: dict[str, Any]) -> FeedbackEvent | None:
    tenant_id = _to_uuid(row.get("tenant_id"))
    event_id = str(row.get("id") or "").strip()
    if tenant_id is None or not event_id:
        return None

    occurred_at = _to_datetime(row.get("timestamp_utc")) or datetime.now(UTC)
    payload_raw = row.get("payload_json")
    payload: dict[str, Any] | str | None
    if isinstance(payload_raw, dict):
        payload = payload_raw
    elif isinstance(payload_raw, str):
        payload = payload_raw
    else:
        payload = None

    return FeedbackEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        chw_id=_to_int(row.get("chw_id")),
        event_type=str(row.get("event_type") or ""),
        inference_mode=str(row.get("inference_mode")) if row.get("inference_mode") is not None else None,
        module_id=_to_uuid(row.get("module_id")),
        question=_extract_question(payload),
        feedback=_extract_feedback(payload),
        answer_excerpt=_extract_answer_excerpt(payload),
        occurred_at=occurred_at,
    )


class ChatFeedbackAggregator:
    def __init__(
        self,
        ch_client: ClickHouseClient,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._ch = ch_client
        self._settings = settings or get_settings()

    async def distinct_tenant_ids(self) -> list[UUID]:
        query = """
        SELECT DISTINCT tenant_id
        FROM coaching_events
        WHERE event_type IN ({positive:String}, {negative:String})
        ORDER BY tenant_id
        """
        parameters = {
            "positive": _POSITIVE_EVENT,
            "negative": _NEGATIVE_EVENT,
        }
        rows = await self._ch.query_rows(query, parameters=parameters)
        tenant_ids: list[UUID] = []
        for row in rows:
            tenant_id = _to_uuid(row.get("tenant_id"))
            if tenant_id is not None:
                tenant_ids.append(tenant_id)
        return tenant_ids

    def resolve_since_ts(
        self,
        *,
        watermark: datetime | None,
        now: datetime | None = None,
    ) -> datetime:
        if watermark is not None:
            return watermark
        reference = now or datetime.now(UTC)
        lookback_days = self._settings.chat_feedback_summary_first_run_lookback_days
        return reference - timedelta(days=lookback_days)

    async def fetch_since(
        self,
        tenant_id: UUID,
        *,
        since_ts: datetime,
    ) -> TenantFeedbackBatch:
        rows = await self._query_events(tenant_id=tenant_id, since_ts=since_ts)
        events: list[FeedbackEvent] = []
        for row in rows:
            event = _row_to_event(row)
            if event is not None:
                events.append(event)
        return TenantFeedbackBatch(tenant_id=tenant_id, events=events)

    async def _query_events(
        self,
        *,
        tenant_id: UUID,
        since_ts: datetime,
    ) -> list[dict[str, Any]]:
        if since_ts.tzinfo is None:
            since_ts = since_ts.replace(tzinfo=UTC)
        query = """
        SELECT
          id,
          tenant_id,
          chw_id,
          event_type,
          inference_mode,
          module_id,
          payload_json,
          timestamp_utc
        FROM coaching_events
        WHERE event_type IN ({positive:String}, {negative:String})
          AND tenant_id = {tenant_id:UUID}
          AND timestamp_utc > {since_ts:DateTime64(3)}
        ORDER BY timestamp_utc ASC
        """
        parameters = {
            "positive": _POSITIVE_EVENT,
            "negative": _NEGATIVE_EVENT,
            "tenant_id": tenant_id,
            "since_ts": since_ts,
        }
        return await self._ch.query_rows(query, parameters=parameters)
