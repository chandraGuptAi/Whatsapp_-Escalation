"""Error types every layer raises; one place turns them into JSON responses."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger("app")


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.status = status or self.status


class Unauthorized(AppError):
    status, code = 401, "unauthorized"


class Forbidden(AppError):
    status, code = 403, "forbidden"


class NotFound(AppError):
    status, code = 404, "not_found"


class Conflict(AppError):
    status, code = 409, "conflict"


class RateLimited(AppError):
    status, code = 429, "rate_limited"


class EscalationError(AppError):
    """Signature of the old service's error, kept so ported engines raise unchanged:
    EscalationError(400, "message")."""

    def __init__(self, status: int, message: str):
        super().__init__(message, status=status, code={400: "bad_request", 404: "not_found", 409: "conflict",
                                                         422: "invalid_request", 503: "unavailable"}.get(status, "error"))


def _body(code: str, message: str, **extra) -> dict:
    return {"error": {"code": code, "message": message, **extra}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError):
        return JSONResponse(_body(exc.code, exc.message), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError):
        fields = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()[:20]]
        return JSONResponse(_body("invalid_request", "Invalid request", fields=fields), status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse(_body("http_error", str(exc.detail)), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        logger.exception("Unhandled request error", exc_info=exc)
        return JSONResponse(_body("internal", "Service temporarily unavailable"), status_code=500)
