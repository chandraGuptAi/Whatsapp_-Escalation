"""Keeps tenants' WhatsApp sessions connected and stores incoming group messages.

Sessions are split across ingest workers by tenant: run N copies with INGEST_SHARDS=N and
INGEST_SHARD=0..N-1. Within a worker each session runs in its own SessionRunner; an advisory
lock per session makes a second owner impossible even if shards are misconfigured.

    INGEST_SHARD=0 INGEST_SHARDS=1 python -m workers.ingest_worker
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from app.core.db import system_transaction, use_selector_loop_on_windows
from app.modules.groups.whatsapp.session import SessionRunner

from .runtime import runtime

logger = logging.getLogger("app.ingest")
RESCAN_SECONDS = 5
RETRY_AFTER = timedelta(seconds=30)
AUTO_RESTART = {"pending", "connected", "disconnected"}  # 'pairing' and 'error' wait for the reconnect button


async def owned_sessions(rt, shard: int, shards: int) -> list[dict]:
    async with system_transaction(rt.db) as conn:
        cur = await conn.execute("SELECT * FROM app.list_wa_sessions(%s, %s)", (shard, shards))
        return await cur.fetchall()


class Supervisor:
    def __init__(self, rt):
        self.rt = rt
        self.running: dict = {}        # session id -> (runner, task)
        self.last_start: dict = {}     # session id -> datetime of the latest start

    def _should_start(self, session: dict, now: datetime) -> bool:
        previous = self.last_start.get(session["session_id"])
        if previous is None:
            return True
        requested = session["restart_requested_at"]
        if requested is not None and requested > previous:
            return True
        return session["status"] in AUTO_RESTART and now - previous > RETRY_AFTER

    async def scan(self, sessions: list[dict]) -> None:
        now = datetime.now(timezone.utc)
        listed = {s["session_id"] for s in sessions}
        for session_id, (runner, task) in list(self.running.items()):
            if task.done():
                if not task.cancelled() and task.exception():
                    logger.error("Session %s stopped", session_id, exc_info=task.exception())
                del self.running[session_id]
            elif session_id not in listed:
                runner.stop()  # logged out or tenant suspended
        for session in sessions:
            current = self.running.get(session["session_id"])
            if current is not None:
                requested = session["restart_requested_at"]
                if requested is not None and requested > current[0].started_at:
                    current[0].stop()  # the next scan starts a fresh runner
                continue
            if self._should_start(session, now):
                runner = SessionRunner(self.rt, session, asyncio.get_running_loop())
                self.last_start[session["session_id"]] = runner.started_at
                self.running[session["session_id"]] = (runner, asyncio.create_task(runner.run()))

    async def stop_all(self) -> None:
        for runner, _ in self.running.values():
            runner.stop()
        tasks = [task for _, task in self.running.values()]
        if tasks:
            await asyncio.wait(tasks, timeout=15)


async def main() -> None:
    shard, shards = int(os.getenv("INGEST_SHARD", "0")), int(os.getenv("INGEST_SHARDS", "1"))
    async with runtime() as rt:
        supervisor = Supervisor(rt)
        logger.info("Ingest worker shard %s/%s started", shard, shards)
        try:
            while not rt.stop.is_set():
                try:
                    await supervisor.scan(await owned_sessions(rt, shard, shards))
                except Exception:
                    logger.exception("Session scan failed")
                try:
                    await asyncio.wait_for(rt.stop.wait(), RESCAN_SECONDS)
                except TimeoutError:
                    pass
        finally:
            await supervisor.stop_all()


if __name__ == "__main__":
    use_selector_loop_on_windows()
    asyncio.run(main())
