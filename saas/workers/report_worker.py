"""Runs module jobs. Scale by starting more copies; SKIP LOCKED keeps them from colliding.

    python -m workers.report_worker
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket

from app.core.modules import discover, job_handlers
from app.core.queue import Worker

from .runtime import runtime

logger = logging.getLogger("app.worker")


async def main() -> None:
    async with runtime() as rt:
        handlers = job_handlers(discover(rt.settings.disabled_modules))
        worker_id = f"{socket.gethostname()}:{os.getpid()}"
        logger.info("Worker %s started for %s", worker_id, sorted({m for m, _ in handlers}))
        await Worker(rt.db, rt.redis, handlers, worker_id=worker_id, concurrency=rt.settings.worker_concurrency,
                     lease_seconds=rt.settings.job_lease_seconds, services=rt.services).run(rt.stop)


if __name__ == "__main__":
    asyncio.run(main())
