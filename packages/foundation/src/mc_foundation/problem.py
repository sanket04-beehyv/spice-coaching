"""RFC 7807 Problem Details HTTP primitives.

Foundation must not import ``mc_contracts``; callers pass string codes
(typically ``ErrorCode.X.value`` from contracts). Problem Details ``type``
is a relative catalog pointer ``docs/error-codes.json#{code}``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"
_ERROR_CATALOG_PATH = "docs/error-codes.json"

INTERNAL_ERROR_CODE = "internal_error"
INTERNAL_ERROR_DETAIL = "An unexpected error occurred."
VALIDATION_ERROR_CODE = "validation_error"


class AppError(Exception):
    """Domain/application error mapped to an RFC 7807 Problem Details response."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        status: int = 400,
        title: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        extensions: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.title = title or _title_from_code(code)
        self.errors = errors
        self.extensions = dict(extensions) if extensions else {}


def _title_from_code(code: str) -> str:
    return code.replace("_", " ").strip().title() or "Error"


def error_type_ref(code: str) -> str:
    """Relative catalog reference for Problem Details ``type``."""
    return f"{_ERROR_CATALOG_PATH}#{code}"


def build_problem_body(
    *,
    code: str,
    detail: str,
    status: int,
    title: str | None = None,
    instance: str | None = None,
    errors: Sequence[Mapping[str, Any]] | None = None,
    extensions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Problem Details dict (RFC 7807 + ``code`` extension)."""
    body: dict[str, Any] = {
        "type": error_type_ref(code),
        "title": title or _title_from_code(code),
        "status": status,
        "detail": detail,
        "code": code,
    }
    if instance is not None:
        body["instance"] = instance
    if errors is not None:
        body["errors"] = list(errors)
    if extensions:
        for key, value in extensions.items():
            if key in body:
                continue
            body[key] = value
    return body


def problem_json_response(
    *,
    code: str,
    detail: str,
    status: int,
    title: str | None = None,
    instance: str | None = None,
    errors: Sequence[Mapping[str, Any]] | None = None,
    extensions: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Return an ``application/problem+json`` response."""
    body = build_problem_body(
        code=code,
        detail=detail,
        status=status,
        title=title,
        instance=instance,
        errors=errors,
        extensions=extensions,
    )
    return JSONResponse(
        status_code=status,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=dict(headers) if headers else None,
    )


def problem_from_app_error(request: Request, exc: AppError) -> JSONResponse:
    return problem_json_response(
        code=exc.code,
        detail=exc.detail,
        status=exc.status,
        title=exc.title,
        instance=str(request.url.path),
        errors=exc.errors,
        extensions=exc.extensions,
    )


def _request_instance(request: Request) -> str:
    return str(request.url.path)


def _http_exception_to_problem(request: Request, status_code: int, detail: Any) -> JSONResponse:
    """Normalize Starlette/FastAPI HTTPException detail into Problem Details."""
    code = _default_code_for_status(status_code)
    message = _default_detail_for_status(status_code)
    extensions: dict[str, Any] = {}
    errors: list[dict[str, Any]] | None = None

    if isinstance(detail, dict):
        raw_code = detail.get("code")
        if isinstance(raw_code, str) and raw_code:
            code = raw_code
        raw_message = detail.get("message")
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
        elif isinstance(detail.get("detail"), str):
            message = detail["detail"]
        elif "status" in detail and "checks" in detail:
            # Legacy readiness payload
            code = "service_unavailable"
            message = "one or more dependencies are unavailable"
            checks = detail.get("checks")
            if isinstance(checks, dict):
                extensions["checks"] = checks
        else:
            # Preserve unknown structured detail as extension when useful
            for key, value in detail.items():
                if key in {"code", "message", "detail", "title", "type", "status", "instance", "errors"}:
                    continue
                extensions[key] = value
            if not isinstance(raw_message, str) and "detail" not in detail:
                message = str(detail)
        raw_errors = detail.get("errors")
        if isinstance(raw_errors, list):
            errors = [e for e in raw_errors if isinstance(e, dict)]
    elif isinstance(detail, str):
        message = detail
    elif detail is not None:
        message = str(detail)

    return problem_json_response(
        code=code,
        detail=message,
        status=status_code,
        instance=_request_instance(request),
        errors=errors,
        extensions=extensions or None,
    )


def _default_code_for_status(status: int) -> str:
    mapping = {
        400: "bad_request",
        401: "not_authenticated",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        413: "payload_too_large",
        422: "validation_error",
        429: "rate_limit_exceeded",
        501: "not_implemented",
        502: "bad_gateway",
        503: "service_unavailable",
    }
    return mapping.get(status, INTERNAL_ERROR_CODE)


def _default_detail_for_status(status: int) -> str:
    mapping = {
        400: "Bad request",
        401: "Not authenticated",
        403: "Forbidden",
        404: "Not found",
        409: "Conflict",
        413: "Payload too large",
        422: "Validation failed",
        429: "Rate limit exceeded",
        501: "Not implemented",
        502: "Bad gateway",
        503: "Service unavailable",
    }
    return mapping.get(status, INTERNAL_ERROR_DETAIL)


def validation_problem_response(request: Request, exc: Any) -> JSONResponse:
    """Map FastAPI ``RequestValidationError`` into one Problem Details body."""
    raw_errors = exc.errors() if callable(getattr(exc, "errors", None)) else []
    errors: list[dict[str, Any]] = []
    for item in raw_errors:
        if not isinstance(item, dict):
            continue
        loc = item.get("loc") or ()
        errors.append(
            {
                "loc": [str(part) if not isinstance(part, (str, int)) else part for part in loc],
                "msg": str(item.get("msg") or "invalid"),
                "type": str(item.get("type") or "value_error"),
            }
        )
    detail = "Request validation failed"
    if errors:
        first = errors[0]
        loc_str = ".".join(str(p) for p in first.get("loc") or ())
        detail = f"{loc_str}: {first.get('msg')}" if loc_str else str(first.get("msg"))
    return problem_json_response(
        code=VALIDATION_ERROR_CODE,
        detail=detail,
        status=422,
        instance=_request_instance(request),
        errors=errors,
    )


def register_problem_handlers(
    app: Any,
    *,
    validation_error_type: type[BaseException] | None = None,
    http_exception_type: type[BaseException] | None = None,
) -> None:
    """Register AppError / validation / HTTPException / catch-all handlers.

    ``validation_error_type`` and ``http_exception_type`` are passed by each
    service (FastAPI types) so foundation stays free of a FastAPI dependency.
    """

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return problem_from_app_error(request, exc)

    if validation_error_type is not None:

        @app.exception_handler(validation_error_type)
        async def _validation_handler(request: Request, exc: BaseException) -> JSONResponse:
            return validation_problem_response(request, exc)

    if http_exception_type is not None:

        @app.exception_handler(http_exception_type)
        async def _http_exc_handler(request: Request, exc: Any) -> JSONResponse:
            status_code = int(getattr(exc, "status_code", 500))
            detail = getattr(exc, "detail", None)
            return _http_exception_to_problem(request, status_code, detail)

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled exception path=%s",
            request.url.path,
            exc_info=exc,
        )
        return problem_json_response(
            code=INTERNAL_ERROR_CODE,
            detail=INTERNAL_ERROR_DETAIL,
            status=500,
            instance=_request_instance(request),
        )


def parse_problem_body(payload: Any, *, fallback_status: int) -> AppError:
    """Parse a Problem Details (or legacy) JSON body into ``AppError``."""
    if not isinstance(payload, dict):
        return AppError(
            _default_code_for_status(fallback_status),
            _default_detail_for_status(fallback_status),
            status=fallback_status,
        )

    code = payload.get("code")
    if not isinstance(code, str) or not code:
        code = _default_code_for_status(fallback_status)

    detail = payload.get("detail")
    if not isinstance(detail, str) or not detail:
        message = payload.get("message")
        detail = (
            message if isinstance(message, str) and message else _default_detail_for_status(fallback_status)
        )

    title = payload.get("title")
    title_str = title if isinstance(title, str) else None

    errors_raw = payload.get("errors")
    errors = [e for e in errors_raw if isinstance(e, dict)] if isinstance(errors_raw, list) else None

    extensions = {
        key: value
        for key, value in payload.items()
        if key not in {"type", "title", "status", "detail", "instance", "code", "errors", "message"}
    }
    status = payload.get("status")
    status_int = int(status) if isinstance(status, int) else fallback_status

    return AppError(
        code,
        detail,
        status=status_int,
        title=title_str,
        errors=errors,
        extensions=extensions or None,
    )
