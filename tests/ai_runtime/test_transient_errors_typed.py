"""Typed SDK error classification for provider retries."""

from __future__ import annotations

from ai_runtime.services.transient_errors import is_transient_provider_error


def test_value_error_is_permanent() -> None:
    assert is_transient_provider_error(ValueError("provider returned no completion choices")) is False
