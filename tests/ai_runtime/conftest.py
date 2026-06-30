"""Shared fixtures for ai-runtime unit tests."""

from __future__ import annotations

import pytest

from ai_runtime.config import get_settings

_TEST_INTERNAL_TOKEN = "test-internal-token-for-unit-tests-only"


@pytest.fixture(autouse=True)
def _ai_runtime_test_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERNAL_TOKEN", _TEST_INTERNAL_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
