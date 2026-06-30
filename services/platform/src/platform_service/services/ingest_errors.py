"""Domain errors for ingest upload validation."""

from __future__ import annotations

from platform_service.exceptions import PlatformError


class IngestValidationError(PlatformError):
    """Invalid ingest upload parameters or file metadata."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
