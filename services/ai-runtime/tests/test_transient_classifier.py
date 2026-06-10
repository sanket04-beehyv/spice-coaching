"""Unit tests for `_is_transient` classification.

Locks in the post-Stage-1-recovery behaviour: RESOURCE_EXHAUSTED (Vertex
quota 429) is now treated as **transient** so the in-call retry loop
can wait through a per-minute quota window. Previously it was permanent
and short-circuited immediately, which caused 24% of pages on the SK
manual smoke run to land as `vision_failed`.

Other permanent markers (4xx auth/argument errors) must still be
classified as permanent — retrying them is wasted budget.
"""

from __future__ import annotations

import pytest
from ai_runtime.services.prompt_executor import _is_transient


class TestTransientClassification:
    """The transient/permanent split is the load-bearing decision in the
    retry loop. Each marker tested explicitly so future edits are forced
    to think about each case."""

    @pytest.mark.parametrize(
        "marker",
        [
            "INVALID_ARGUMENT",
            "PERMISSION_DENIED",
            "UNAUTHENTICATED",
            "NOT_FOUND",
            "FAILED_PRECONDITION",
            " 400 ",
            " 401 ",
            " 403 ",
            " 404 ",
        ],
    )
    def test_known_permanent_markers_are_not_transient(self, marker: str) -> None:
        exc = RuntimeError(f"some prefix {marker} some suffix")
        assert _is_transient(exc) is False

    def test_resource_exhausted_is_now_transient(self) -> None:
        """The behaviour change. Before: permanent (no retry, immediate
        fail). After: transient (the retry loop's longer backoffs span a
        full per-minute Vertex quota window)."""
        exc = RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded for project ...")
        assert _is_transient(exc) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "Connection reset by peer",
            "Read timed out",
            "503 Service Unavailable",
            "500 Internal Server Error",
            "ECONNRESET",
            "asyncio.exceptions.TimeoutError",
        ],
    )
    def test_transport_errors_are_transient(self, msg: str) -> None:
        assert _is_transient(RuntimeError(msg)) is True

    def test_bare_exception_with_unknown_message_defaults_transient(self) -> None:
        """The classifier defaults to transient for unknown messages —
        the comment in the source explains: 'we err on the side of
        retrying — if we mis-classify a permanent error as transient
        we just waste a few seconds before returning it'."""
        assert _is_transient(RuntimeError("something went sideways")) is True
