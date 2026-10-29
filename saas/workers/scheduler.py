"""Turns due schedules into jobs. Safe to run more than one copy: schedules are claimed with
SKIP LOCKED and each run's idempotency key includes its due time, so a run is queued once.

    python -m workers.scheduler
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.core.db import system_transaction, tenant_transaction
from app.core.queue import enqueue
from app.core.reports.presets import next_run

from .runtime import runtime

logger = logging.getLogger("app.scheduler")
TICK_SECONDS = 30
BATCH = 50


def run_options(preset_options: dict, due_at: datetime) -> dict:
    """Presets store a relative window ("window_hours": 24); a run needs the concrete window."""
    options = dict(preset_options)
    hours = int(options.pop("window_hours", 24))
    options["window_end"] = due_at.isoformat()
    options["window_start"] = (due_at - timedelta(hours=hours)).isoformat()
    return options


async def tick(rt) -> int:
    async with system_transaction(rt.db) as conn:
        cur = await conn.execute("SELECT * FROM app.claim_due_schedules(%s)", (BATCH,))
        due = await cur.fetchall()
    for item in due:
        try:
            async with tenant_transaction(rt.db, item["tenant_id"]) as conn:
                await enqueue(conn, tenant_id=item["tenant_id"], module_key=item["module_key"], kind="generate",
                              idempotency_key=f"schedule:{item['schedule_id']}:{item['due_at'].isoformat()}",
                              options=run_options(item["options"] or {}, item["due_at"]),
                              schedule_id=item["schedule_id"])
                await conn.execute(
                    "UPDATE app.schedules SET last_run_at = now(), next_run_at = %s WHERE id = %s",
                    (next_run(item["cron"], item["timezone"]), item["schedule_id"]),
                )
        except Exception:
            logger.exception("Schedule %s failed to enqueue; it will be retried after its lease", item["schedule_id"])
    return len(due)


async def main() -> None:
    async with runtime() as rt:
        while not rt.stop.is_set():
            if await tick(rt) == BATCH:
                continue
            try:
                await asyncio.wait_for(rt.stop.wait(), TICK_SECONDS)
            except TimeoutError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
