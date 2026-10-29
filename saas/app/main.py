"""API process. Stateless: run as many copies as needed behind a load balancer.

    uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers 4
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from .core import cache
from .core.auth import routes as auth_routes
from .core.config import Settings, get_settings
from .core.db import Database
from .core.errors import install_error_handlers
from .core.jobs import routes as job_routes
from .core.modules import discover, mount
from .core.observability import RequestIdMiddleware, configure_logging
from .core.reports import presets as preset_routes
from .core.reports import routes as report_routes
from .core.services import build_services


def create_app(settings: Settings | None = None, *, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()
    modules = discover(settings.disabled_modules)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = Database(settings.database_url, min_size=settings.db_pool_min, max_size=settings.db_pool_max)
        await app.state.db.open()
        app.state.redis = await cache.connect(settings.redis_url, required=settings.env not in ("dev", "test"))
        app.state.http = http_client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        try:
            yield
        finally:
            if http_client is None:
                await app.state.http.aclose()
            if app.state.redis is not None:
                await app.state.redis.aclose()
            await app.state.db.close()

    app = FastAPI(title="WhatsApp Escalation", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.services = build_services(settings)
    app.state.modules = modules
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(auth_routes.router)
    app.include_router(job_routes.router)
    app.include_router(report_routes.router)
    app.include_router(preset_routes.router)
    mount(app, modules)

    @app.get("/health", include_in_schema=False)
    async def health():
        return {"status": "ok", "modules": [m.key for m in modules]}

    return app
