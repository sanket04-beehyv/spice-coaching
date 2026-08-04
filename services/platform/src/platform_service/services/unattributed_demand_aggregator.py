"""Fetch and dedupe unattributed digital_help / module_requested evidence from ClickHouse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.services.question_text import normalize_question

_SOURCE_DIGITAL_HELP = "digital_help"
_SOURCE_MODULE_REQUESTED = "module_requested"


@dataclass(frozen=True)
class DedupedEvidence:
    source: str
    text: str
    normalized_text: str
    occurrence_count: int
    last_seen_at: datetime | None
    sample_event_id: str | None
    sample_chw_id: int | None


def _to_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return None


class UnattributedDemandAggregator:
    """Query unattributed_module_demand_events (falls back to coaching_events)."""

    def __init__(self, ch_client: ClickHouseClient) -> None:
        self._ch = ch_client

    async def fetch_for_day(
        self,
        *,
        tenant_id: UUID | None,
        event_date: date,
    ) -> tuple[list[DedupedEvidence], list[DedupedEvidence]]:
        """Return (questions, requests) deduped by normalized text."""
        try:
            rows = await self._query_mv(tenant_id=tenant_id, event_date=event_date)
        except Exception:
            rows = await self._query_coaching_events(tenant_id=tenant_id, event_date=event_date)
        return self._dedupe(rows)

    async def list_tenant_ids_for_day(self, *, event_date: date) -> list[UUID]:
        query = """
        SELECT DISTINCT tenant_id
        FROM unattributed_module_demand_events
        WHERE event_date = {event_date:Date}
        """
        try:
            rows = await self._ch.query_rows(query, parameters={"event_date": event_date})
        except Exception:
            query = """
            SELECT DISTINCT tenant_id
            FROM coaching_events
            WHERE event_date = {event_date:Date}
              AND module_id IS NULL
              AND (
                event_type = 'digital_help_used'
                OR (
                  event_type = 'module_requested'
                  AND length(trimBoth(JSONExtractString(payload_json, 'requested_module_name'))) > 0
                )
              )
            """
            rows = await self._ch.query_rows(query, parameters={"event_date": event_date})
        out: list[UUID] = []
        for row in rows:
            tid = _to_uuid(row.get("tenant_id"))
            if tid is not None:
                out.append(tid)
        return out

    async def _query_mv(
        self,
        *,
        tenant_id: UUID | None,
        event_date: date,
    ) -> list[dict[str, Any]]:
        tenant_clause = ""
        parameters: dict[str, Any] = {"event_date": event_date}
        if tenant_id is not None:
            tenant_clause = "  AND tenant_id = {tenant_id:UUID}\n"
            parameters["tenant_id"] = tenant_id
        query = f"""
        SELECT
          id,
          tenant_id,
          chw_id,
          source,
          text,
          normalized_text,
          timestamp_utc
        FROM unattributed_module_demand_events
        WHERE event_date = {{event_date:Date}}
        {tenant_clause}
        """
        return await self._ch.query_rows(query, parameters=parameters)

    async def _query_coaching_events(
        self,
        *,
        tenant_id: UUID | None,
        event_date: date,
    ) -> list[dict[str, Any]]:
        tenant_clause = ""
        parameters: dict[str, Any] = {"event_date": event_date}
        if tenant_id is not None:
            tenant_clause = "  AND tenant_id = {tenant_id:UUID}\n"
            parameters["tenant_id"] = tenant_id
        query = f"""
        SELECT
          id,
          tenant_id,
          chw_id,
          if(event_type = 'digital_help_used', 'digital_help', 'module_requested') AS source,
          if(
            event_type = 'digital_help_used',
            coalesce(
              nullIf(JSONExtractString(payload_json, 'question'), ''),
              nullIf(JSONExtractString(payload_json, 'query'), '')
            ),
            nullIf(JSONExtractString(payload_json, 'requested_module_name'), '')
          ) AS text,
          lowerUTF8(
            replaceRegexpAll(
              trimBoth(
                if(
                  event_type = 'digital_help_used',
                  coalesce(
                    nullIf(JSONExtractString(payload_json, 'question'), ''),
                    nullIf(JSONExtractString(payload_json, 'query'), '')
                  ),
                  nullIf(JSONExtractString(payload_json, 'requested_module_name'), '')
                )
              ),
              '\\\\s+',
              ' '
            )
          ) AS normalized_text,
          timestamp_utc
        FROM coaching_events
        WHERE event_date = {{event_date:Date}}
          AND module_id IS NULL
          AND (
            (
              event_type = 'digital_help_used'
              AND length(
                trimBoth(
                  coalesce(
                    nullIf(JSONExtractString(payload_json, 'question'), ''),
                    nullIf(JSONExtractString(payload_json, 'query'), '')
                  )
                )
              ) > 0
            )
            OR (
              event_type = 'module_requested'
              AND length(trimBoth(JSONExtractString(payload_json, 'requested_module_name'))) > 0
            )
          )
        {tenant_clause}
        """
        return await self._ch.query_rows(query, parameters=parameters)

    def _dedupe(
        self,
        rows: list[dict[str, Any]],
    ) -> tuple[list[DedupedEvidence], list[DedupedEvidence]]:
        questions: dict[str, DedupedEvidence] = {}
        requests: dict[str, DedupedEvidence] = {}

        for row in rows:
            source = str(row.get("source") or "").strip()
            raw_text = str(row.get("text") or "").strip()
            if not raw_text:
                continue
            normalized = str(row.get("normalized_text") or "").strip() or normalize_question(raw_text)
            if not normalized:
                continue
            last_seen = _to_datetime(row.get("timestamp_utc"))
            event_id = str(row.get("id") or "") or None
            chw_id = _to_int(row.get("chw_id"), default=0) or None
            bucket = questions if source == _SOURCE_DIGITAL_HELP else requests
            if source not in (_SOURCE_DIGITAL_HELP, _SOURCE_MODULE_REQUESTED):
                continue
            existing = bucket.get(normalized)
            if existing is None:
                bucket[normalized] = DedupedEvidence(
                    source=source,
                    text=raw_text,
                    normalized_text=normalized,
                    occurrence_count=1,
                    last_seen_at=last_seen,
                    sample_event_id=event_id,
                    sample_chw_id=chw_id,
                )
                continue
            new_last = last_seen
            if existing.last_seen_at is not None and (new_last is None or existing.last_seen_at >= new_last):
                new_last = existing.last_seen_at
                raw_text = existing.text
                event_id = existing.sample_event_id
                chw_id = existing.sample_chw_id
            bucket[normalized] = DedupedEvidence(
                source=source,
                text=raw_text,
                normalized_text=normalized,
                occurrence_count=existing.occurrence_count + 1,
                last_seen_at=new_last,
                sample_event_id=event_id,
                sample_chw_id=chw_id,
            )

        def _sorted(items: dict[str, DedupedEvidence]) -> list[DedupedEvidence]:
            return sorted(items.values(), key=lambda e: e.occurrence_count, reverse=True)

        return _sorted(questions), _sorted(requests)
