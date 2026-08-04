"""Finalize must not complete runs while a step is awaiting_input."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from platform_service.services.run_state.steps import RunStepMixin
from platform_service.services.run_state_service import (
    RUN_RUNNING,
    STAGE_CARD_DRAFT,
    STAGE_THUMBNAIL,
    STEP_AWAITING_INPUT,
    STEP_SUCCEEDED,
)


class _Harness(RunStepMixin):
    def __init__(self) -> None:
        self._session = MagicMock()
        self.get_run = AsyncMock()
        self.list_steps = AsyncMock()
        self.complete_run = AsyncMock()


@pytest.mark.asyncio
async def test_maybe_finalize_blocks_on_awaiting_input() -> None:
    harness = _Harness()
    run_id = uuid4()
    harness.get_run.return_value = SimpleNamespace(id=run_id, status=RUN_RUNNING)
    harness.list_steps.return_value = [
        SimpleNamespace(stage=STAGE_THUMBNAIL, status=STEP_SUCCEEDED),
        SimpleNamespace(stage=STAGE_CARD_DRAFT, status=STEP_AWAITING_INPUT),
    ]

    assert await harness.maybe_finalize_ingestion_run(run_id) is False
    harness.complete_run.assert_not_awaited()
