"""Domain errors for ingest upload validation."""

from __future__ import annotations

from mc_contracts.errors import ErrorCode
from mc_foundation.problem import AppError


class IngestValidationError(AppError):
    """Invalid ingest upload parameters or file metadata."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = ErrorCode.BAD_REQUEST.value,
    ) -> None:
        super().__init__(code, message, status=status_code)
        self.message = message
        self.status_code = status_code
