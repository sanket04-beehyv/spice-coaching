"""Fetch raw chat question candidates from ClickHouse digital_help_used events."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.config import Settings, get_settings

_DIGITAL_HELP_EVENT = "digital_help_used"
_FAQ_ID_NAMESPACE = uuid.UUID("a3f2c8e1-4b6d-4e9a-8f1c-2d7e6b5a4c93")

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class CandidateQuestion:
    text: str
    normalized_text: str
    occurrence_count: int
    last_seen_at: datetime | None


@dataclass(frozen=True)
class TenantQuestionCandidates:
    tenant_id: UUID
    questions: list[CandidateQuestion]


def normalize_question(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip())


def stable_faq_id(*, tenant_id: UUID, normalized_question_en: str) -> UUID:
    return uuid.uuid5(_FAQ_ID_NAMESPACE, f"{tenant_id}:{normalized_question_en}")


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


class ChatFaqAggregator:
    def __init__(
        self,
        ch_client: ClickHouseClient,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._ch = ch_client
        self._settings = settings or get_settings()

    async def fetch_candidates(self) -> list[TenantQuestionCandidates]:
        settings = self._settings
        raw_rows = await self._query_clickhouse(
            lookback_days=settings.chat_faq_lookback_days,
            min_occurrence_count=settings.chat_faq_min_occurrence_count,
            min_question_length=settings.chat_faq_min_question_length,
            candidate_limit=settings.chat_faq_cluster_candidate_limit,
        )
        return self._group_candidates(
            raw_rows,
            min_question_length=settings.chat_faq_min_question_length,
        )

    async def _query_clickhouse(
        self,
        *,
        lookback_days: int,
        min_occurrence_count: int,
        min_question_length: int,
        candidate_limit: int,
    ) -> list[dict[str, Any]]:
        query = """
        SELECT
          tenant_id,
          question_text,
          occurrence_count,
          last_seen_at
        FROM (
          SELECT
            tenant_id,
            trimBoth(
              coalesce(
                nullIf(JSONExtractString(payload_json, 'question'), ''),
                nullIf(JSONExtractString(payload_json, 'query'), '')
              )
            ) AS question_text,
            count() AS occurrence_count,
            max(timestamp_utc) AS last_seen_at
          FROM coaching_events
          WHERE event_type = {event_type:String}
            AND event_date >= (today() - toIntervalDay({lookback_days:Int32}))
          GROUP BY tenant_id, question_text
          HAVING occurrence_count >= {min_occurrence_count:Int32}
            AND length(question_text) >= {min_question_length:Int32}
          ORDER BY occurrence_count DESC
          LIMIT {candidate_limit:Int32} BY tenant_id
        )
        """
        parameters = {
            "event_type": _DIGITAL_HELP_EVENT,
            "lookback_days": int(lookback_days),
            "min_occurrence_count": int(min_occurrence_count),
            "min_question_length": int(min_question_length),
            "candidate_limit": int(candidate_limit),
        }
        return await self._ch.query_rows(query, parameters=parameters)

    def _group_candidates(
        self,
        raw_rows: list[dict[str, Any]],
        *,
        min_question_length: int,
    ) -> list[TenantQuestionCandidates]:
        by_tenant: dict[UUID, dict[str, tuple[str, int, datetime | None]]] = {}

        for row in raw_rows:
            tenant_id = _to_uuid(row.get("tenant_id"))
            if tenant_id is None:
                continue
            question = str(row.get("question_text") or "")
            normalized = normalize_question(question)
            if len(normalized) < min_question_length:
                continue
            occurrence_count = _to_int(row.get("occurrence_count"))
            last_seen_at = _to_datetime(row.get("last_seen_at"))

            merged = by_tenant.setdefault(tenant_id, {})
            existing = merged.get(normalized)
            if existing is None:
                merged[normalized] = (normalize_question(question), occurrence_count, last_seen_at)
                continue
            display, total_count, existing_last_seen = existing
            total_count += occurrence_count
            if last_seen_at is not None and (existing_last_seen is None or last_seen_at > existing_last_seen):
                display = normalize_question(question)
                existing_last_seen = last_seen_at
            merged[normalized] = (display, total_count, existing_last_seen)

        results: list[TenantQuestionCandidates] = []
        for tenant_id, merged in by_tenant.items():
            questions = [
                CandidateQuestion(
                    text=display,
                    normalized_text=normalized,
                    occurrence_count=count,
                    last_seen_at=last_seen_at,
                )
                for normalized, (display, count, last_seen_at) in merged.items()
            ]
            questions.sort(key=lambda q: q.occurrence_count, reverse=True)
            if questions:
                results.append(TenantQuestionCandidates(tenant_id=tenant_id, questions=questions))
        return results
