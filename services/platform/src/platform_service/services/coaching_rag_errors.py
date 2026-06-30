"""Domain errors for coaching RAG retrieval and generation."""

from __future__ import annotations

from platform_service.exceptions import PlatformError


class CoachingRagError(PlatformError):
    """RAG query failed at embed, retrieval, or generation."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
