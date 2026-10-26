"""Private service entrypoint; keep the public escalation URL contract."""
import hmac
import os
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from starlette.responses import JSONResponse

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from .escalation_analyst.application import create_app
from .escalation_analyst.settings import Settings


class ServiceAuthentication:
    def __init__(self, app, secret: str):
        self.app, self.secret = app, secret

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            supplied = dict(scope["headers"]).get(b"x-escalation-service-token", b"")
            if not hmac.compare_digest(self.secret.encode(), supplied):
                return await JSONResponse({"error": "Invalid service credentials"}, 401)(scope, receive, send)
        await self.app(scope, receive, send)


def build_app(settings=None, *, service_token=None):
    if settings is None:
        settings = Settings()
        if not settings.database_url:
            raise RuntimeError("ESCALATION_DATABASE_URL must be configured for PostgreSQL storage")
    secret = service_token if service_token is not None else os.getenv("ESCALATION_SERVICE_TOKEN", "").strip()
    if not secret:
        raise RuntimeError("ESCALATION_SERVICE_TOKEN must be configured")
    module = create_app(settings)

    @asynccontextmanager
    async def lifespan(app):
        async with module.router.lifespan_context(module):
            yield

    host = FastAPI(title="WhatsApp service", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    host.add_middleware(ServiceAuthentication, secret=secret)

    @host.get("/health")
    async def health():
        return {"status": "ok", "service": "whatsapp-escalation"}

    host.mount("/api/escalation", module)
    return host


# Use uvicorn app.main:build_app --factory, one worker only.
