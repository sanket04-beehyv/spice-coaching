"""Document usage analytics — ClickHouse MV + event drill-down with org enrichment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from mc_contracts.dashboard import (
    DocumentUsageDocumentRow,
    DocumentUsageEventRow,
    DocumentUsageResponse,
    DocumentUsageTopItem,
)
from sqlalchemy.ext.asyncio import AsyncSession

from platform_service.clickhouse.client import ClickHouseClient
from platform_service.db.repositories.source_repository import SourceRepository
from platform_service.services.document_usage_hierarchy import (
    OrgUser,
    apply_document_usage_filters,
    org_user_index,
    resolve_visible_chw_ids,
    user_display,
)

_DOCUMENT_VIEWED = "document_viewed"
_DEFAULT_TOP_LIMIT = 10


@dataclass(frozen=True, slots=True)
class DocumentUsageFilter:
    from_date: date
    to_date: date
    tenant_id: UUID | None = None
    upazila: str | None = None
    district: str | None = None
    po_id: int | None = None
    sk_id: int | None = None
    user_id: int | None = None
    document_id: UUID | None = None
    viewer_id: int | None = None
    unrestricted_viewer: bool = False


def _to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return None


class DocumentUsageAnalyticsService:
    def __init__(
        self,
        ch_client: ClickHouseClient,
        session: AsyncSession | None = None,
    ) -> None:
        self._ch = ch_client
        self._session = session

    def _resolved_chw_ids(self, filters: DocumentUsageFilter) -> frozenset[int] | None:
        visible = resolve_visible_chw_ids(
            filters.viewer_id,
            unrestricted=filters.unrestricted_viewer,
        )
        return apply_document_usage_filters(
            visible,
            po_id=filters.po_id,
            sk_id=filters.sk_id,
            user_id=filters.user_id,
            district=filters.district,
            upazila=filters.upazila,
        )

    def _mv_where(
        self,
        filters: DocumentUsageFilter,
        chw_ids: frozenset[int] | None,
    ) -> tuple[str, dict[str, Any]]:
        clauses = [
            "event_date >= {from_date:Date}",
            "event_date <= {to_date:Date}",
            "source_document_id != ''",
        ]
        params: dict[str, Any] = {
            "from_date": filters.from_date,
            "to_date": filters.to_date,
        }
        if filters.tenant_id is not None:
            clauses.append("tenant_id = {tenant_id:UUID}")
            params["tenant_id"] = filters.tenant_id
        if filters.document_id is not None:
            clauses.append("source_document_id = {document_id:String}")
            params["document_id"] = str(filters.document_id)
        if chw_ids is not None:
            clauses.append("chw_id IN {chw_ids:Array(Int64)}")
            params["chw_ids"] = list(chw_ids)
        return " AND ".join(clauses), params

    def _events_where(
        self,
        filters: DocumentUsageFilter,
        chw_ids: frozenset[int] | None,
    ) -> tuple[str, dict[str, Any]]:
        clauses = [
            "event_type = {event_type:String}",
            "event_date >= {from_date:Date}",
            "event_date <= {to_date:Date}",
            "JSONExtractString(payload_json, 'source_document_id') != ''",
        ]
        params: dict[str, Any] = {
            "event_type": _DOCUMENT_VIEWED,
            "from_date": filters.from_date,
            "to_date": filters.to_date,
        }
        if filters.tenant_id is not None:
            clauses.append("tenant_id = {tenant_id:UUID}")
            params["tenant_id"] = filters.tenant_id
        if filters.document_id is not None:
            clauses.append("JSONExtractString(payload_json, 'source_document_id') = {document_id:String}")
            params["document_id"] = str(filters.document_id)
        if chw_ids is not None:
            clauses.append("chw_id IN {chw_ids:Array(Int64)}")
            params["chw_ids"] = list(chw_ids)
        return " AND ".join(clauses), params

    async def _title_map(self, document_ids: list[UUID]) -> dict[UUID, str]:
        if not document_ids or self._session is None:
            return {}
        docs = await SourceRepository(self._session).list_source_documents_by_ids(document_ids)
        return {doc.id: doc.title for doc in docs}

    def _empty_response(
        self,
        filters: DocumentUsageFilter,
        *,
        documents_limit: int,
        documents_offset: int,
        events_limit: int,
        events_offset: int,
    ) -> DocumentUsageResponse:
        return DocumentUsageResponse(
            from_date=filters.from_date,
            to_date=filters.to_date,
            total_views=0,
            unique_documents=0,
            unique_users=0,
            top_documents=[],
            total_document_rows=0,
            documents=[],
            total_events=0,
            events=[],
            documents_limit=documents_limit,
            documents_offset=documents_offset,
            events_limit=events_limit,
            events_offset=events_offset,
        )

    async def get_usage(
        self,
        filters: DocumentUsageFilter,
        *,
        top_limit: int = _DEFAULT_TOP_LIMIT,
        documents_limit: int = 20,
        documents_offset: int = 0,
        events_limit: int = 50,
        events_offset: int = 0,
    ) -> DocumentUsageResponse:
        """Return KPIs, per-document rows, and event drill-down under one filter set."""
        chw_ids = self._resolved_chw_ids(filters)
        if chw_ids is not None and len(chw_ids) == 0:
            return self._empty_response(
                filters,
                documents_limit=documents_limit,
                documents_offset=documents_offset,
                events_limit=events_limit,
                events_offset=events_offset,
            )

        where_sql, params = self._mv_where(filters, chw_ids)
        events_where, events_params = self._events_where(filters, chw_ids)

        summary_rows = await self._ch.query_rows(
            f"""
            SELECT
              sum(view_count) AS total_views,
              uniqExact(source_document_id) AS unique_documents,
              uniqExact(chw_id) AS unique_users
            FROM document_view_daily
            WHERE {where_sql}
            """,
            parameters=params,
        )
        summary = summary_rows[0] if summary_rows else {}
        total_views = _to_int(summary.get("total_views"))
        unique_documents = _to_int(summary.get("unique_documents"))
        unique_users = _to_int(summary.get("unique_users"))

        top_rows = await self._ch.query_rows(
            f"""
            SELECT
              source_document_id,
              sum(view_count) AS view_count
            FROM document_view_daily
            WHERE {where_sql}
            GROUP BY source_document_id
            ORDER BY view_count DESC
            LIMIT {{top_limit:UInt32}}
            """,
            parameters={**params, "top_limit": int(top_limit)},
        )

        count_rows = await self._ch.query_rows(
            f"""
            SELECT uniqExact(source_document_id) AS total_document_rows
            FROM document_view_daily
            WHERE {where_sql}
            """,
            parameters=params,
        )
        total_document_rows = _to_int((count_rows[0] if count_rows else {}).get("total_document_rows"))

        agg_rows = await self._ch.query_rows(
            f"""
            SELECT
              source_document_id,
              sum(view_count) AS total_views,
              uniqExact(chw_id) AS unique_users
            FROM document_view_daily
            WHERE {where_sql}
            GROUP BY source_document_id
            ORDER BY total_views DESC
            LIMIT {{documents_limit:UInt32}} OFFSET {{documents_offset:UInt32}}
            """,
            parameters={
                **params,
                "documents_limit": int(documents_limit),
                "documents_offset": int(documents_offset),
            },
        )

        page_ids: list[UUID] = []
        page_meta: list[tuple[UUID, int, int]] = []
        for row in agg_rows:
            doc_id = _to_uuid(row.get("source_document_id"))
            if doc_id is None:
                continue
            page_ids.append(doc_id)
            page_meta.append((doc_id, _to_int(row.get("total_views")), _to_int(row.get("unique_users"))))

        last_by_doc: dict[UUID, tuple[int | None, datetime | None]] = {}
        if page_ids:
            last_rows = await self._ch.query_rows(
                f"""
                SELECT
                  JSONExtractString(payload_json, 'source_document_id') AS source_document_id,
                  argMax(chw_id, timestamp_utc) AS last_chw_id,
                  max(timestamp_utc) AS last_viewed_at
                FROM coaching_events
                WHERE {events_where}
                  AND JSONExtractString(payload_json, 'source_document_id')
                      IN {{page_doc_ids:Array(String)}}
                GROUP BY source_document_id
                """,
                parameters={
                    **events_params,
                    "page_doc_ids": [str(d) for d in page_ids],
                },
            )
            for row in last_rows:
                doc_id = _to_uuid(row.get("source_document_id"))
                if doc_id is None:
                    continue
                last_chw = _to_int(row.get("last_chw_id"), default=-1)
                last_by_doc[doc_id] = (
                    last_chw if last_chw >= 0 else None,
                    _as_datetime(row.get("last_viewed_at")),
                )

        event_count_rows = await self._ch.query_rows(
            f"""
            SELECT count() AS total_events
            FROM coaching_events
            WHERE {events_where}
            """,
            parameters=events_params,
        )
        total_events = _to_int((event_count_rows[0] if event_count_rows else {}).get("total_events"))

        event_rows = await self._ch.query_rows(
            f"""
            SELECT
              id AS event_id,
              JSONExtractString(payload_json, 'source_document_id') AS source_document_id,
              chw_id,
              upazila_id,
              timestamp_utc AS viewed_at
            FROM coaching_events
            WHERE {events_where}
            ORDER BY timestamp_utc DESC
            LIMIT {{events_limit:UInt32}} OFFSET {{events_offset:UInt32}}
            """,
            parameters={
                **events_params,
                "events_limit": int(events_limit),
                "events_offset": int(events_offset),
            },
        )

        title_ids = list(
            dict.fromkeys(
                [
                    *(_to_uuid(r.get("source_document_id")) for r in top_rows),
                    *page_ids,
                    *(_to_uuid(r.get("source_document_id")) for r in event_rows),
                ]
            )
        )
        titles = await self._title_map([d for d in title_ids if d is not None])
        users: dict[int, OrgUser] = org_user_index()

        top_documents: list[DocumentUsageTopItem] = []
        for row in top_rows:
            doc_id = _to_uuid(row.get("source_document_id"))
            if doc_id is None:
                continue
            top_documents.append(
                DocumentUsageTopItem(
                    document_id=doc_id,
                    document_title=titles.get(doc_id),
                    view_count=_to_int(row.get("view_count")),
                )
            )

        documents: list[DocumentUsageDocumentRow] = []
        for doc_id, total_doc_views, unique_doc_users in page_meta:
            last_chw_id, last_viewed_at = last_by_doc.get(doc_id, (None, None))
            display = user_display(last_chw_id, users) if last_chw_id is not None else {}
            documents.append(
                DocumentUsageDocumentRow(
                    document_id=doc_id,
                    document_title=titles.get(doc_id),
                    total_views=total_doc_views,
                    unique_users=unique_doc_users,
                    last_viewed_at=last_viewed_at,
                    last_viewed_by_user_id=last_chw_id,
                    last_viewed_by_user_name=display.get("user_name") if display else None,
                )
            )

        events: list[DocumentUsageEventRow] = []
        for row in event_rows:
            doc_id = _to_uuid(row.get("source_document_id"))
            if doc_id is None:
                continue
            user_id = _to_int(row.get("chw_id"), default=-1)
            if user_id < 0:
                continue
            display = user_display(user_id, users)
            event_upazila = row.get("upazila_id")
            events.append(
                DocumentUsageEventRow(
                    event_id=str(row.get("event_id") or ""),
                    document_id=doc_id,
                    document_title=titles.get(doc_id),
                    user_id=user_id,
                    user_name=display["user_name"],
                    user_role=display["user_role"],
                    upazila_id=str(event_upazila) if event_upazila else display["upazila"],
                    district=display["district"],
                    viewed_at=_as_datetime(row.get("viewed_at")),
                )
            )

        return DocumentUsageResponse(
            from_date=filters.from_date,
            to_date=filters.to_date,
            total_views=total_views,
            unique_documents=unique_documents,
            unique_users=unique_users,
            top_documents=top_documents,
            total_document_rows=total_document_rows,
            documents=documents,
            total_events=total_events,
            events=events,
            documents_limit=documents_limit,
            documents_offset=documents_offset,
            events_limit=events_limit,
            events_offset=events_offset,
        )
