"""Safe and consistent HTTP/SSE error responses."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pymongo.errors import PyMongoError

from service.request_context import get_request_id

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


def _code_for_status(status_code: int) -> str:
    return {
        status.HTTP_400_BAD_REQUEST: "bad_request",
        status.HTTP_401_UNAUTHORIZED: "unauthorized",
        status.HTTP_403_FORBIDDEN: "forbidden",
        status.HTTP_404_NOT_FOUND: "not_found",
        status.HTTP_409_CONFLICT: "conflict",
        status.HTTP_413_CONTENT_TOO_LARGE: "request_too_large",
        status.HTTP_422_UNPROCESSABLE_CONTENT: "validation_error",
        status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
        status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
    }.get(status_code, "internal_error" if status_code >= 500 else "http_error")


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", get_request_id())


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    details: Any | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=request_id,
            details=details,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(exclude_none=True),
        headers=headers,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    status_code = exc.status_code
    message = "Internal server error" if status_code >= 500 else str(exc.detail)
    return _error_response(
        status_code=status_code,
        code=_code_for_status(status_code),
        message=message,
        request_id=_request_id(request),
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    # Do not include the rejected input values because they may contain secrets.
    details = [
        {
            "location": list(error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return _error_response(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation_error",
        message="Request validation failed",
        request_id=_request_id(request),
        details=details,
    )


async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = _request_id(request)
    logger.exception(
        "Unhandled request exception request_id=%s path=%s",
        request_id,
        request.url.path,
        exc_info=exc,
    )
    return _error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        code="internal_error",
        message="Internal server error",
        request_id=request_id,
    )


async def database_exception_handler(request: Request, exc: PyMongoError) -> JSONResponse:
    request_id = _request_id(request)
    logger.exception(
        "SCAT database request failed request_id=%s path=%s",
        request_id,
        request.url.path,
        exc_info=exc,
    )
    return _error_response(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="database_unavailable",
        message="SCAT database is temporarily unavailable",
        request_id=request_id,
    )


def install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_exception_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(
        PyMongoError,
        database_exception_handler,  # type: ignore[arg-type]
    )
    app.add_exception_handler(Exception, unexpected_exception_handler)


def sse_error_event(message: str, *, code: str = "internal_error") -> dict[str, str]:
    """Build a backward-compatible structured SSE error event."""
    return {
        "type": "error",
        "content": message,
        "code": code,
        "request_id": get_request_id(),
    }
