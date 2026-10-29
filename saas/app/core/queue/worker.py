"""Postgres-backed job runner. Jobs are claimed with FOR UPDATE SKIP LOCKED, so any number
of worker processes can run side by side without double-processing a job."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import UUID

from ..db import Database, system_transaction, tenant_transaction
from ..events import publish_job_event

logger = logging.getLogger("app.worker")

MAX_ATTEMPTS = 3
IDLE_POLL_SECONDS = 2.0


class PermanentJobError(Exception):
    """Raise from a handler when retrying cannot help (bad input, missing data)."""


class JobCancelled(PermanentJobError):
    """The user cancelled the job; raised from JobContext.progress()."""


class JobEmpty(PermanentJobError):
    """Nothing to process (e.g. no messages in the window); the job ends as 'empty'."""


@dataclass
class JobContext:
    db: Database
    redis: object | None
    services: dict
    job_id: UUID
    tenant_id: UUID
    module_key: str
    kind: str
    options: dict
    attempts: int

    def tenant(self):
        return tenant_transaction(self.db, self.tenant_id)

    async def progress(self, percent: int, message: str = "") -> None:
        """Report progress; also the cancellation check, so call it between steps."""
        percent = max(0, min(99, percent))
        async with self.tenant() as conn:
            cur = await conn.execute(
                "UPDATE app.jobs SET progress = %s WHERE id = %s AND status = 'running' RETURNING id",
                (percent, self.job_id),
            )
            if await cur.fetchone() is None:
                raise JobCancelled("Job was cancelled")
        await publish_job_event(self.redis, self.tenant_id, self.job_id,
                                {"status": "running", "progress": percent, "message": message})


JobHandler = Callable[[JobContext], Awaitable[None]]


class Worker:
    def __init__(self, db: Database, redis, handlers: dict[tuple[str, str], JobHandler], *,
                 worker_id: str, concurrency: int, lease_seconds: int, services: dict | None = None):
        self.db, self.redis, self.handlers = db, redis, handlers
        self.worker_id, self.concurrency, self.lease_seconds = worker_id, concurrency, lease_seconds
        self.services = services or {}
        self.modules = sorted({module for module, _ in handlers})

    async def run(self, stop: asyncio.Event) -> None:
        slots = asyncio.Semaphore(self.concurrency)
        running: set[asyncio.Task] = set()
        while not stop.is_set():
            await slots.acquire()
            job = await self._claim()
            if job is None:
                slots.release()
                try:
                    await asyncio.wait_for(stop.wait(), IDLE_POLL_SECONDS)
                except TimeoutError:
                    pass
                continue
            task = asyncio.create_task(self._execute(job, slots))
            running.add(task)
            task.add_done_callback(running.discard)
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def _claim(self) -> dict | None:
        async with system_transaction(self.db) as conn:
            cur = await conn.execute("SELECT * FROM app.claim_job(%s, %s, %s)",
                                     (self.worker_id, self.lease_seconds, self.modules))
            return await cur.fetchone()

    async def _execute(self, job: dict, slots: asyncio.Semaphore) -> None:
        ctx = JobContext(db=self.db, redis=self.redis, services=self.services, job_id=job["job_id"],
                         tenant_id=job["tenant_id"], module_key=job["module_key"], kind=job["kind"],
                         options=job["options"] or {}, attempts=job["attempts"])
        status, error = "complete", None
        try:
            handler = self.handlers.get((ctx.module_key, ctx.kind))
            if handler is None:
                raise PermanentJobError(f"No handler for {ctx.module_key}.{ctx.kind}")
            await handler(ctx)
        except asyncio.CancelledError:
            status, error = "queued", "worker stopped"
            raise
        except PermanentJobError as exc:
            status, error = ("empty" if isinstance(exc, JobEmpty) else "failed"), str(exc)
        except Exception as exc:
            logger.exception("Job %s failed (attempt %s)", ctx.job_id, ctx.attempts)
            status = "queued" if ctx.attempts < MAX_ATTEMPTS else "failed"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                await self._finish(ctx, status, error)
            finally:
                slots.release()

    async def _finish(self, ctx: JobContext, status: str, error: str | None) -> None:
        async with ctx.tenant() as conn:
            await conn.execute(
                """UPDATE app.jobs
                      SET status = %s, error = %s, locked_by = NULL, locked_until = NULL,
                          progress = CASE WHEN %s = 'complete' THEN 100 ELSE progress END,
                          finished_at = CASE WHEN %s IN ('complete', 'empty', 'failed') THEN now() END
                    WHERE id = %s AND status = 'running'""",
                (status, (error or "")[:2000] or None, status, status, ctx.job_id),
            )
        await publish_job_event(self.redis, ctx.tenant_id, ctx.job_id, {"status": status, "error": error})
