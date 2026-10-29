"""Keeps tenants' WhatsApp sessions connected and stores incoming group messages.

Sessions are split across ingest workers by tenant: run N copies with INGEST_SHARDS=N and
INGEST_SHARD=0..N-1. Each session is owned by exactly one shard.

    INGEST_SHARD=0 INGEST_SHARDS=1 python -m workers.ingest_worker
"""
from __future__ import annotations

import asyncio
import logging
import os

from app.core.db import system_transaction, use_selector_loop_on_windows

from .runtime import runtime

logger = logging.getLogger("app.ingest")
RESCAN_SECONDS = 60


async def owned_sessions(rt, shard: int, shards: int) -> list[dict]:
    async with system_transaction(rt.db) as conn:
        cur = await conn.execute("SELECT * FROM app.list_wa_sessions(%s, %s)", (shard, shards))
        return await cur.fetchall()


async def run_session(rt, session: dict) -> None:
    """Connect one tenant's WhatsApp session and store each group message with
    app.modules.groups.repository.insert_message inside tenant_transaction(session tenant).

    Not ported yet: the neonize client lives in app/escalation_analyst/whatsapp/native.py of the
    current service and must be adapted to load/save wa_sessions.session_enc via core.security.Cipher."""
    raise NotImplementedError("WhatsApp client not ported yet")


async def main() -> None:
    shard, shards = int(os.getenv("INGEST_SHARD", "0")), int(os.getenv("INGEST_SHARDS", "1"))
    async with runtime() as rt:
        tasks: dict = {}
        while not rt.stop.is_set():
            for session in await owned_sessions(rt, shard, shards):
                task = tasks.get(session["session_id"])
                if task is None or task.done():
                    if task is not None and task.exception():
                        logger.error("Session %s stopped: %s", session["session_id"], task.exception())
                    tasks[session["session_id"]] = asyncio.create_task(run_session(rt, session))
            try:
                await asyncio.wait_for(rt.stop.wait(), RESCAN_SECONDS)
            except TimeoutError:
                pass
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


if __name__ == "__main__":
    use_selector_loop_on_windows()
    asyncio.run(main())
