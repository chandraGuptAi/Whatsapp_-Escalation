"""JSON logs with a request id and tenant id on every line."""
from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar

from ..tenancy import current_principal_var

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        principal = current_principal_var.get()
        entry = {"level": record.levelname, "logger": record.name, "msg": record.getMessage(),
                 "request_id": request_id_var.get(), "tenant_id": str(principal.tenant_id) if principal else None}
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


class RequestIdMiddleware:
    """Pure ASGI (works with streaming responses); echoes X-Request-Id."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        supplied = dict(scope["headers"]).get(b"x-request-id", b"").decode()[:64]
        request_id = supplied or uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
