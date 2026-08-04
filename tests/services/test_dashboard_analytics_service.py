"""Unit tests for DashboardAnalyticsService."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.services.dashboard_analytics_service import DashboardAnalyticsService


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_ranks_by_combined_count() -> None:
    """Combined rank: 3+8 (=11) beats 10+0; request-only module is included."""
    module_combined = uuid4()
    module_chat_only = uuid4()
    module_request_only = uuid4()
    family_combined = uuid4()
    family_chat = uuid4()
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(
        return_value=[
            {
                "module_id": str(module_combined),
                "digital_help_count": 3,
                "module_requested_count": 8,
            },
            {
                "module_id": str(module_chat_only),
                "digital_help_count": 10,
                "module_requested_count": 0,
            },
            {
                "module_id": str(module_request_only),
                "digital_help_count": 0,
                "module_requested_count": 5,
            },
        ]
    )
    session = MagicMock()
    combined_mod = MagicMock(
        id=module_combined,
        module_family_id=family_combined,
        title_localized={"bn": "Combined BN", "en": "Combined EN"},
    )
    chat_mod = MagicMock(
        id=module_chat_only,
        module_family_id=family_chat,
        title_localized={"bn": "Chat BN"},
    )

    with patch.object(ModuleRepository, "list_modules_by_ids", new_callable=AsyncMock) as mock_by_ids:
        mock_by_ids.return_value = [combined_mod, chat_mod]
        result = await DashboardAnalyticsService(ch_mock, session).get_digital_help_module_usage(
            tenant_id=None,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
            limit=20,
        )

    assert result.from_date == date(2026, 1, 1)
    assert result.to_date == date(2026, 1, 31)
    assert result.total_digital_help == 13
    assert result.total_module_requested == 13
    assert result.total_modules == 3
    assert result.limit == 20
    assert result.offset == 0
    assert len(result.modules) == 3
    assert result.modules[0].module_id == module_combined
    assert result.modules[0].module_family_id == family_combined
    assert result.modules[0].digital_help_count == 3
    assert result.modules[0].module_requested_count == 8
    assert result.modules[0].title == {"bn": "Combined BN", "en": "Combined EN"}
    assert result.modules[1].module_id == module_chat_only
    assert result.modules[1].digital_help_count == 10
    assert result.modules[1].module_requested_count == 0
    assert result.modules[2].module_id == module_request_only
    assert result.modules[2].digital_help_count == 0
    assert result.modules[2].module_requested_count == 5


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_empty_window() -> None:
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(return_value=[])

    result = await DashboardAnalyticsService(ch_mock, None).get_digital_help_module_usage(
        tenant_id=None,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 7),
        limit=20,
    )

    assert result.total_digital_help == 0
    assert result.total_module_requested == 0
    assert result.total_modules == 0
    assert result.modules == []


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_passes_tenant_id_to_clickhouse() -> None:
    tenant_id = uuid4()
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(return_value=[])

    await DashboardAnalyticsService(ch_mock, None).get_digital_help_module_usage(
        tenant_id=tenant_id,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 14),
        limit=5,
    )

    assert len(ch_mock.query_rows.await_args_list) == 1
    call = ch_mock.query_rows.await_args_list[0]
    assert call.kwargs["parameters"]["tenant_id"] == tenant_id
    assert call.kwargs["parameters"]["from_date"] == date(2026, 1, 1)
    assert call.kwargs["parameters"]["to_date"] == date(2026, 1, 14)
    assert call.kwargs["parameters"]["digital_help"] == "digital_help_used"
    assert call.kwargs["parameters"]["module_requested"] == "module_requested"


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_ignores_null_module_id_rows() -> None:
    """Family-only / free-text events (module_id NULL) must not appear in the ranking."""
    module_id = uuid4()
    family_id = uuid4()
    ch_mock = MagicMock()
    # ClickHouse query already filters module_id IS NOT NULL; service also skips None.
    ch_mock.query_rows = AsyncMock(
        return_value=[
            {
                "module_id": str(module_id),
                "digital_help_count": 5,
                "module_requested_count": 2,
            },
            {
                "module_id": None,
                "digital_help_count": 99,
                "module_requested_count": 50,
            },
        ]
    )
    session = MagicMock()
    module_row = MagicMock(
        id=module_id,
        module_family_id=family_id,
        title_localized={"bn": "Only BN", "en": "Only EN"},
    )

    with patch.object(ModuleRepository, "list_modules_by_ids", new_callable=AsyncMock) as mock_by_ids:
        mock_by_ids.return_value = [module_row]
        result = await DashboardAnalyticsService(ch_mock, session).get_digital_help_module_usage(
            tenant_id=None,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
            limit=20,
        )

    assert result.total_digital_help == 5
    assert result.total_module_requested == 2
    assert len(result.modules) == 1
    assert result.modules[0].module_id == module_id
    assert result.modules[0].module_family_id == family_id
    assert result.modules[0].digital_help_count == 5
    assert result.modules[0].module_requested_count == 2


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_paginates_modules() -> None:
    modules = [uuid4() for _ in range(3)]
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(
        return_value=[
            {
                "module_id": str(modules[0]),
                "digital_help_count": 20,
                "module_requested_count": 10,
            },
            {
                "module_id": str(modules[1]),
                "digital_help_count": 15,
                "module_requested_count": 5,
            },
            {
                "module_id": str(modules[2]),
                "digital_help_count": 10,
                "module_requested_count": 0,
            },
        ]
    )
    session = MagicMock()

    with patch.object(ModuleRepository, "list_modules_by_ids", new_callable=AsyncMock) as mock_by_ids:
        mock_by_ids.return_value = []
        result = await DashboardAnalyticsService(ch_mock, session).get_digital_help_module_usage(
            tenant_id=None,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
            limit=1,
            offset=1,
        )

    assert result.total_modules == 3
    assert result.total_digital_help == 45
    assert result.total_module_requested == 15
    assert result.limit == 1
    assert result.offset == 1
    assert len(result.modules) == 1
    assert result.modules[0].module_id == modules[1]
    assert result.modules[0].digital_help_count == 15
    assert result.modules[0].module_requested_count == 5


@pytest.mark.asyncio
async def test_get_digital_help_module_questions_paginates_and_skips_blank() -> None:
    module_id = uuid4()
    last_asked = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    ch_mock = MagicMock()

    async def _query_rows(query: str, parameters: dict | None = None) -> list[dict]:
        if "total_questions" in query:
            return [{"total_questions": 2}]
        return [
            {
                "question": "How to treat fever?",
                "occurrence_count": 3,
                "last_asked_at": last_asked,
            },
            {
                "question": "  ",
                "occurrence_count": 1,
                "last_asked_at": datetime(2026, 1, 10, tzinfo=UTC),
            },
        ]

    ch_mock.query_rows = AsyncMock(side_effect=_query_rows)
    session = MagicMock()
    module_row = MagicMock(id=module_id, title_localized={"bn": "Fever BN", "en": "Fever EN"})

    with patch.object(ModuleRepository, "list_modules_by_ids", new_callable=AsyncMock) as mock_by_ids:
        mock_by_ids.return_value = [module_row]
        result = await DashboardAnalyticsService(ch_mock, session).get_digital_help_module_questions(
            module_id=module_id,
            tenant_id=None,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
            limit=50,
            offset=0,
        )

    assert result.module_id == module_id
    assert result.title == {"bn": "Fever BN", "en": "Fever EN"}
    assert result.total_questions == 2
    assert result.total_pages == 1
    assert len(result.questions) == 1
    assert result.questions[0].question == "How to treat fever?"
    assert result.questions[0].occurrence_count == 3
    assert result.questions[0].last_asked_at == last_asked
    assert ch_mock.query_rows.await_count == 2
    page_call = ch_mock.query_rows.await_args_list[1]
    assert page_call.kwargs["parameters"]["module_id"] == module_id
    assert page_call.kwargs["parameters"]["event_type"] == "digital_help_used"
    assert "module_id = {module_id:UUID}" in page_call.args[0]


@pytest.mark.asyncio
async def test_get_digital_help_module_questions_empty_window() -> None:
    module_id = uuid4()
    ch_mock = MagicMock()

    async def _query_rows(query: str, parameters: dict | None = None) -> list[dict]:
        if "total_questions" in query:
            return [{"total_questions": 0}]
        return []

    ch_mock.query_rows = AsyncMock(side_effect=_query_rows)

    result = await DashboardAnalyticsService(ch_mock, None).get_digital_help_module_questions(
        module_id=module_id,
        tenant_id=None,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 7),
        limit=50,
        offset=0,
    )

    assert result.title is None
    assert result.total_questions == 0
    assert result.total_pages == 0
    assert result.questions == []


@pytest.mark.asyncio
async def test_get_digital_help_module_questions_passes_tenant_id() -> None:
    module_id = uuid4()
    tenant_id = uuid4()
    ch_mock = MagicMock()

    async def _query_rows(query: str, parameters: dict | None = None) -> list[dict]:
        if "total_questions" in query:
            return [{"total_questions": 0}]
        return []

    ch_mock.query_rows = AsyncMock(side_effect=_query_rows)

    await DashboardAnalyticsService(ch_mock, None).get_digital_help_module_questions(
        module_id=module_id,
        tenant_id=tenant_id,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 14),
        limit=10,
        offset=5,
    )

    for call in ch_mock.query_rows.await_args_list:
        assert call.kwargs["parameters"]["tenant_id"] == tenant_id
        assert "tenant_id = {tenant_id:UUID}" in call.args[0]


@pytest.mark.asyncio
async def test_get_digital_help_module_requests_returns_count_and_title() -> None:
    module_id = uuid4()
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(return_value=[{"module_requested_count": 7}])
    session = MagicMock()
    module_row = MagicMock(id=module_id, title_localized={"bn": "Req BN"})

    with patch.object(ModuleRepository, "list_modules_by_ids", new_callable=AsyncMock) as mock_by_ids:
        mock_by_ids.return_value = [module_row]
        result = await DashboardAnalyticsService(ch_mock, session).get_digital_help_module_requests(
            module_id=module_id,
            tenant_id=None,
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
        )

    assert result.module_id == module_id
    assert result.module_requested_count == 7
    assert result.title == {"bn": "Req BN"}
    call = ch_mock.query_rows.await_args_list[0]
    assert call.kwargs["parameters"]["module_id"] == module_id
    assert call.kwargs["parameters"]["event_type"] == "module_requested"
    assert "module_id = {module_id:UUID}" in call.args[0]


@pytest.mark.asyncio
async def test_get_digital_help_module_requests_zero_when_empty() -> None:
    module_id = uuid4()
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(return_value=[])

    result = await DashboardAnalyticsService(ch_mock, None).get_digital_help_module_requests(
        module_id=module_id,
        tenant_id=None,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 7),
    )

    assert result.module_requested_count == 0
    assert result.title is None


@pytest.mark.asyncio
async def test_get_digital_help_module_requests_passes_tenant_id() -> None:
    module_id = uuid4()
    tenant_id = uuid4()
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(return_value=[{"module_requested_count": 0}])

    await DashboardAnalyticsService(ch_mock, None).get_digital_help_module_requests(
        module_id=module_id,
        tenant_id=tenant_id,
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 14),
    )

    call = ch_mock.query_rows.await_args_list[0]
    assert call.kwargs["parameters"]["tenant_id"] == tenant_id
    assert "tenant_id = {tenant_id:UUID}" in call.args[0]
