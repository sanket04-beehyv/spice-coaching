"""Unit tests for DashboardAnalyticsService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from platform_service.db.repositories.module_repository import ModuleRepository
from platform_service.services.dashboard_analytics_service import DashboardAnalyticsService


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_ranks_by_query_count() -> None:
    family_high = uuid4()
    family_low = uuid4()
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(
        side_effect=[
            [
                {"module_family_id": str(family_low), "query_count": 3},
                {"module_family_id": str(family_high), "query_count": 10},
            ],
            [],
        ]
    )
    session = MagicMock()
    module_high_id = uuid4()
    module_low_id = uuid4()
    title_mod = MagicMock(id=module_high_id, title_localized={"bn": "High BN", "en": "High EN"})
    low_mod = MagicMock(id=module_low_id, title_localized={"bn": "Low BN"})

    with (
        patch.object(
            ModuleRepository,
            "list_latest_published_by_family_ids",
            new_callable=AsyncMock,
        ) as mock_titles,
    ):
        mock_titles.return_value = {
            family_high: title_mod,
            family_low: low_mod,
        }
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
    assert result.modules[0].module_family_id == family_high
    assert result.modules[0].module_id == module_high_id
    assert result.modules[0].query_count == 10
    assert result.modules[0].title == {"bn": "High BN", "en": "High EN"}
    assert result.modules[1].module_family_id == family_low
    assert result.modules[1].module_id == module_low_id
    assert result.modules[1].query_count == 3


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_empty_window() -> None:
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(side_effect=[[], []])

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
    ch_mock.query_rows = AsyncMock(side_effect=[[], []])

    await DashboardAnalyticsService(ch_mock, None).get_digital_help_module_usage(
        tenant_id=tenant_id,
        period_days=14,
        limit=5,
    )

    for call in ch_mock.query_rows.await_args_list:
        assert call.kwargs["parameters"]["tenant_id"] == tenant_id
        assert call.kwargs["parameters"]["period_days"] == 14


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_merges_module_id_only_rows() -> None:
    family_id = uuid4()
    module_id = uuid4()
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(
        side_effect=[
            [],
            [{"module_id": str(module_id), "query_count": 5}],
        ]
    )
    session = MagicMock()
    module_row = MagicMock(
        id=module_id, module_family_id=family_id, title_localized={"bn": "Merged BN", "en": "Merged EN"}
    )

    with (
        patch.object(ModuleRepository, "list_modules_by_ids", new_callable=AsyncMock) as mock_by_ids,
        patch.object(
            ModuleRepository,
            "list_latest_published_by_family_ids",
            new_callable=AsyncMock,
        ) as mock_titles,
    ):
        mock_by_ids.return_value = [module_row]
        mock_titles.return_value = {family_id: module_row}
        result = await DashboardAnalyticsService(ch_mock, session).get_digital_help_module_usage(
            tenant_id=None,
            period_days=30,
            limit=20,
        )

    assert result.total_queries == 5
    assert len(result.modules) == 1
    assert result.modules[0].module_family_id == family_id
    assert result.modules[0].module_id == module_id
    assert result.modules[0].query_count == 5
    assert result.modules[0].title == {"bn": "Merged BN", "en": "Merged EN"}


@pytest.mark.asyncio
async def test_get_digital_help_module_usage_paginates_modules() -> None:
    families = [uuid4() for _ in range(3)]
    ch_mock = MagicMock()
    ch_mock.query_rows = AsyncMock(
        side_effect=[
            [
                {"module_family_id": str(families[0]), "query_count": 30},
                {"module_family_id": str(families[1]), "query_count": 20},
                {"module_family_id": str(families[2]), "query_count": 10},
            ],
            [],
        ]
    )
    session = MagicMock()

    with patch.object(
        ModuleRepository,
        "list_latest_published_by_family_ids",
        new_callable=AsyncMock,
    ) as mock_titles:
        mock_titles.return_value = {}
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
    assert result.modules[0].module_family_id == families[1]
    assert result.modules[0].query_count == 20
