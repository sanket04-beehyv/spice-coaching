"""Domain errors for coaching RAG retrieval and generation."""

from __future__ import annotations

from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError


class CoachingRagError(AppError):
    """RAG query failed at embed, retrieval, or generation."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        code: str = ErrorCode.COACHING_RAG_ERROR.value,
    ) -> None:
        super().__init__(code, message, status=status_code)
        self.message = message
        self.status_code = status_code
