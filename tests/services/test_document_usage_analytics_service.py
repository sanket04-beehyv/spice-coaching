"""Document usage analytics service tests (mocked ClickHouse)."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from platform_service.services.document_usage_analytics_service import (
    DocumentUsageAnalyticsService,
    DocumentUsageFilter,
)

pytestmark = pytest.mark.asyncio


def _filters(**overrides: object) -> DocumentUsageFilter:
    base: dict[str, object] = {
        "from_date": date(2026, 4, 1),
        "to_date": date(2026, 4, 30),
        "unrestricted_viewer": True,
    }
    base.update(overrides)
    return DocumentUsageFilter(**base)  # type: ignore[arg-type]


class TestDocumentUsageAnalyticsService:
    async def test_usage_empty_when_chw_filter_empty(self) -> None:
        ch = MagicMock()
        ch.query_rows = AsyncMock()
        service = DocumentUsageAnalyticsService(ch)
        result = await service.get_usage(_filters(viewer_id=401, unrestricted_viewer=False, po_id=999999))
        assert result.total_views == 0
        assert result.unique_documents == 0
        assert result.documents == []
        assert result.events == []
        ch.query_rows.assert_not_called()

    async def test_usage_combines_summary_documents_and_events(self) -> None:
        doc_a = uuid4()
        doc_b = uuid4()
        viewed = datetime(2026, 4, 28, 12, 0, 0)
        ch = MagicMock()
        ch.query_rows = AsyncMock(
            side_effect=[
                # summary
                [{"total_views": 5, "unique_documents": 2, "unique_users": 3}],
                # top
                [
                    {"source_document_id": str(doc_a), "view_count": 3},
                    {"source_document_id": str(doc_b), "view_count": 2},
                ],
                # document row count
                [{"total_document_rows": 2}],
                # document page
                [
                    {
                        "source_document_id": str(doc_a),
                        "total_views": 3,
                        "unique_users": 2,
                    }
                ],
                # last viewed
                [
                    {
                        "source_document_id": str(doc_a),
                        "last_chw_id": 395,
                        "last_viewed_at": viewed,
                    }
                ],
                # event count
                [{"total_events": 1}],
                # events page
                [
                    {
                        "event_id": "evt-1",
                        "source_document_id": str(doc_a),
                        "chw_id": 401,
                        "upazila_id": "Lalmonirhat Sadar",
                        "viewed_at": viewed,
                    }
                ],
            ]
        )
        service = DocumentUsageAnalyticsService(ch)
        result = await service.get_usage(_filters())
        assert result.total_views == 5
        assert result.unique_documents == 2
        assert result.unique_users == 3
        assert [item.document_id for item in result.top_documents] == [doc_a, doc_b]
        assert result.top_documents[0].view_count == 3
        assert result.total_document_rows == 2
        assert result.documents[0].last_viewed_by_user_id == 395
        assert result.documents[0].last_viewed_by_user_name
        assert result.total_events == 1
        assert result.events[0].user_role == "PO"
        assert result.events[0].viewed_at == viewed
