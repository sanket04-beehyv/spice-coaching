"""Typed SDK error classification for provider retries."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from ai_runtime.services.transient_errors import is_transient_provider_error


def test_value_error_is_permanent() -> None:
    assert is_transient_provider_error(ValueError("OpenAI returned no completion choices")) is False


def test_openai_rate_limit_is_transient() -> None:
    pytest.importorskip("openai")
    from openai import RateLimitError

    response = httpx.Response(
        429, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )
    exc = RateLimitError("rate limited", response=response, body=None)
    assert is_transient_provider_error(exc) is True


def test_openai_bad_request_is_permanent() -> None:
    pytest.importorskip("openai")
    from openai import BadRequestError

    response = httpx.Response(
        400, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    )
    exc = BadRequestError("bad request", response=response, body=None)
    assert is_transient_provider_error(exc) is False


def test_openai_status_error_uses_status_code() -> None:
    pytest.importorskip("openai")
    from openai import APIStatusError

    response = MagicMock()
    response.status_code = 503
    exc = APIStatusError("unavailable", response=response, body=None)
    assert is_transient_provider_error(exc) is True
