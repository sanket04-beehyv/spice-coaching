"""Shared fixtures for integration tests.

Lifted out of ``test_pipeline_e2e.py`` so the AV E2E test can use the
same fixtures via pytest's normal discovery (no cross-test-file
imports, which trip ruff's F811 redefinition rule).
"""

from collections.abc import Callable

import pytest


@pytest.fixture
def patch_count_pages(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], None]:
    """Patch ``count_pages`` so Stage A's page-count step returns ``n``.

    Lets the orchestrator step past its zero-pages early-return branch
    without needing a real PDF/audio file on disk.
    """

    def _set(n: int) -> None:
        monkeypatch.setattr(
            "platform_service.workers.stage_a_extract.count_pages",
            lambda *_args, **_kwargs: n,
        )

    return _set
