"""Integration tests for PromptExecutor transient retry loop."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from ai_runtime.services.prompt_executor import _call_with_transient_retry


@pytest.mark.asyncio
async def test_transient_retry_succeeds_on_second_attempt() -> None:
    operation = AsyncMock(side_effect=[ConnectionError("temporary"), "ok"])
    with patch("ai_runtime.services.prompt_executor.asyncio.sleep", new=AsyncMock()):
        result = await _call_with_transient_retry(
            log_context="test",
            provider_name="google",
            model="gemini",
            operation=operation,
        )
    assert result == "ok"
    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_permanent_error_not_retried() -> None:
    operation = AsyncMock(side_effect=ValueError("PERMISSION_DENIED: denied"))
    with patch("ai_runtime.services.prompt_executor.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        with pytest.raises(ValueError, match="PERMISSION_DENIED"):
            await _call_with_transient_retry(
                log_context="test",
                provider_name="google",
                model="gemini",
                operation=operation,
            )
    assert operation.await_count == 1
    sleep_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_retry_exhausts_backoffs() -> None:
    operation = AsyncMock(side_effect=TimeoutError("timeout"))
    with patch("ai_runtime.services.prompt_executor.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        with pytest.raises(TimeoutError):
            await _call_with_transient_retry(
                log_context="test",
                provider_name="google",
                model="gemini",
                operation=operation,
            )
    assert operation.await_count == 4
    assert sleep_mock.await_count == 3
