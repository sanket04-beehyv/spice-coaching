"""Parametrized checks that deployment locale config drives prompt and sync behavior."""

from __future__ import annotations

import pytest
from mc_contracts.localized import LocaleConfig
from mc_contracts.sync import ConfigSyncBundle
from mc_foundation.locale import get_locale_metadata
from platform_service.config import Settings
from platform_service.services.prompts.symbol_verbalization import render_symbol_verbalization_rules


@pytest.mark.parametrize(
    ("primary", "expected_primary_snippet"),
    [
        ("hi", "20 से 30"),
        ("bn", "20 থেকে 30"),
        ("ta", "20 முதல் 30"),
    ],
)
def test_symbol_verbalization_rules_follow_deployment_locale(
    primary: str,
    expected_primary_snippet: str,
) -> None:
    rules = render_symbol_verbalization_rules(primary_locale=primary)
    assert expected_primary_snippet in rules
    assert get_locale_metadata(primary).display_name in rules


@pytest.mark.parametrize(
    ("primary", "expected_supported"),
    [
        ("hi", ["hi"]),
        ("bn", ["bn"]),
        ("te", ["te"]),
    ],
)
def test_deployment_locale_config_supported_locales(
    primary: str,
    expected_supported: list[str],
) -> None:
    settings = Settings(deployment_primary_locale=primary)
    config = settings.deployment_locale_config
    assert config.primary == primary
    assert config.supported == expected_supported


@pytest.mark.parametrize(
    ("primary", "expected_supported"),
    [
        ("hi", ["hi"]),
        ("bn", ["bn"]),
        ("te", ["te"]),
    ],
)
def test_config_sync_bundle_locales_match_deployment(
    primary: str,
    expected_supported: list[str],
) -> None:
    settings = Settings(deployment_primary_locale=primary)
    bundle = ConfigSyncBundle(
        thresholds={},
        locales=settings.deployment_locale_config,
        server_time_utc="2026-01-01T00:00:00Z",
    )
    assert isinstance(bundle.locales, LocaleConfig)
    assert bundle.locales.primary == primary
    assert bundle.locales.supported == expected_supported
