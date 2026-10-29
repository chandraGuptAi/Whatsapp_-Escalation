"""Shared startup for worker processes: settings, logging, DB pool, Redis, services, and a
stop event set on SIGINT/SIGTERM so in-flight jobs finish before exit."""
from __future__ import annotations

import asyncio
import signal
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.core import cache
from app.core.config import Settings, get_settings
from app.core.db import Database
from app.core.observability import configure_logging
from app.core.services import build_services


@dataclass
class Runtime:
    settings: Settings
    db: Database
    redis: object | None
    services: dict
    stop: asyncio.Event


@asynccontextmanager
async def runtime():
    settings = get_settings()
    configure_logging()
    db = Database(settings.database_url, min_size=1, max_size=settings.worker_concurrency + 2)
    await db.open()
    redis = await cache.connect(settings.redis_url, required=settings.env != "dev")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))  # Windows
    try:
        yield Runtime(settings, db, redis, build_services(settings), stop)
    finally:
        if redis is not None:
            await redis.aclose()
        await db.close()
