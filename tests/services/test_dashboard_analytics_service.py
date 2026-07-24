"""Unit tests for DashboardAnalyticsService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.services.dashboard_analytics_service import DashboardAnalyticsService


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_ranks_by_query_count() -> None:
    module_high = uuid4()
    module_low = uuid4()
    family_high = uuid4()
    family_low = uuid4()
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(
        return_value=[
            {"module_id": str(module_low), "query_count": 3},
            {"module_id": str(module_high), "query_count": 10},
        ]
    )
    session = MagicMock()
    title_mod = MagicMock(
        id=module_high,
        module_family_id=family_high,
        title_localized={"bn": "High BN", "en": "High EN"},
    )
    low_mod = MagicMock(
        id=module_low,
        module_family_id=family_low,
        title_localized={"bn": "Low BN"},
    )

    with patch.object(ModuleRepository, "list_modules_by_ids", new_callable=AsyncMock) as mock_by_ids:
        mock_by_ids.return_value = [title_mod, low_mod]
        result = await DashboardAnalyticsService(ch_mock, session).get_digital_help_module_usage(
            tenant_id=None,
            period_days=30,
            limit=20,
        )

    assert result.period_days == 30
    assert result.total_queries == 13
    assert result.total_modules == 2
    assert result.limit == 20
    assert result.offset == 0
    assert len(result.modules) == 2
    assert result.modules[0].module_id == module_high
    assert result.modules[0].module_family_id == family_high
    assert result.modules[0].query_count == 10
    assert result.modules[0].title == {"bn": "High BN", "en": "High EN"}
    assert result.modules[1].module_id == module_low
    assert result.modules[1].module_family_id == family_low
    assert result.modules[1].query_count == 3


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_empty_window() -> None:
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(return_value=[])

    result = await DashboardAnalyticsService(ch_mock, None).get_digital_help_module_usage(
        tenant_id=None,
        period_days=7,
        limit=20,
    )

    assert result.total_queries == 0
    assert result.total_modules == 0
    assert result.modules == []


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_passes_tenant_id_to_clickhouse() -> None:
    tenant_id = uuid4()
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(return_value=[])

    await DashboardAnalyticsService(ch_mock, None).get_digital_help_module_usage(
        tenant_id=tenant_id,
        period_days=14,
        limit=5,
    )

    assert len(ch_mock.query_rows.await_args_list) == 1
    call = ch_mock.query_rows.await_args_list[0]
    assert call.kwargs["parameters"]["tenant_id"] == tenant_id
    assert call.kwargs["parameters"]["period_days"] == 14


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_ignores_null_module_id_rows() -> None:
    """Family-only events (module_id NULL) must not appear in the ranking."""
    module_id = uuid4()
    family_id = uuid4()
    ch_mock = MagicMock()
    # ClickHouse query already filters module_id IS NOT NULL; service also skips None.
    ch_mock.query_rows = AsyncMock(
        return_value=[
            {"module_id": str(module_id), "query_count": 5},
            {"module_id": None, "query_count": 99},
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
            period_days=30,
            limit=20,
        )

    assert result.total_queries == 5
    assert len(result.modules) == 1
    assert result.modules[0].module_id == module_id
    assert result.modules[0].module_family_id == family_id
    assert result.modules[0].query_count == 5


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_paginates_modules() -> None:
    modules = [uuid4() for _ in range(3)]
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(
        return_value=[
            {"module_id": str(modules[0]), "query_count": 30},
            {"module_id": str(modules[1]), "query_count": 20},
            {"module_id": str(modules[2]), "query_count": 10},
        ]
    )
    session = MagicMock()

    with patch.object(ModuleRepository, "list_modules_by_ids", new_callable=AsyncMock) as mock_by_ids:
        mock_by_ids.return_value = []
        result = await DashboardAnalyticsService(ch_mock, session).get_digital_help_module_usage(
            tenant_id=None,
            period_days=30,
            limit=1,
            offset=1,
        )

    assert result.total_modules == 3
    assert result.limit == 1
    assert result.offset == 1
    assert len(result.modules) == 1
    assert result.modules[0].module_id == modules[1]
    assert result.modules[0].query_count == 20
