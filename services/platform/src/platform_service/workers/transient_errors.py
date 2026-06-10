"""Transient error types for Celery autoretry on post-publish workers."""

from __future__ import annotations

import httpx
from sqlalchemy.exc import DBAPIError, OperationalError

CELERY_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    OperationalError,
    DBAPIError,
    httpx.TimeoutException,
    httpx.TransportError,
    httpx.HTTPStatusError,
    ConnectionError,
    TimeoutError,
    OSError,
)
