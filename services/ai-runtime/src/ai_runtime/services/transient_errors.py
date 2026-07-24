"""Classify provider exceptions as transient (retry) or permanent (fail fast)."""

from __future__ import annotations

import asyncio

# String markers for exceptions without typed SDK wrappers (fallback only).
_PERMANENT_ERROR_MARKERS = (
    "INVALID_ARGUMENT",
    "PERMISSION_DENIED",
    "UNAUTHENTICATED",
    "NOT_FOUND",
    "FAILED_PRECONDITION",
    " 400 ",
    " 401 ",
    " 403 ",
    " 404 ",
    "code': 400",
    "code': 401",
    "code': 403",
    "code': 404",
)

_PERMANENT_HTTP_STATUS_CODES = frozenset({400, 401, 403, 404, 422})
_TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _status_code_from_exc(exc: Exception) -> int | None:
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _is_transient_by_message(exc: Exception) -> bool:
    msg = str(exc) or type(exc).__name__
    return not any(m in msg for m in _PERMANENT_ERROR_MARKERS)


def is_transient_provider_error(exc: Exception) -> bool:
    """Return True when the error may succeed on retry."""
    if isinstance(exc, (ValueError, TypeError)):
        return False

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True

    try:
        from google.api_core import exceptions as google_exceptions

        if isinstance(exc, google_exceptions.InvalidArgument):
            return False
        if isinstance(exc, google_exceptions.PermissionDenied):
            return False
        if isinstance(exc, google_exceptions.Unauthenticated):
            return False
        if isinstance(exc, google_exceptions.NotFound):
            return False
        if isinstance(exc, google_exceptions.FailedPrecondition):
            return False
        if isinstance(exc, google_exceptions.ResourceExhausted):
            return True
        if isinstance(exc, google_exceptions.ServiceUnavailable):
            return True
        if isinstance(exc, google_exceptions.DeadlineExceeded):
            return True
        if isinstance(exc, google_exceptions.GoogleAPICallError):
            code = _status_code_from_exc(exc)
            if code in _PERMANENT_HTTP_STATUS_CODES:
                return False
            if code in _TRANSIENT_HTTP_STATUS_CODES:
                return True
    except ImportError:
        pass

    status = _status_code_from_exc(exc)
    if status is not None:
        if status in _PERMANENT_HTTP_STATUS_CODES:
            return False
        if status in _TRANSIENT_HTTP_STATUS_CODES:
            return True

    return _is_transient_by_message(exc)
