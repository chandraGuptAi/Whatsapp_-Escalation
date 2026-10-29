"""Job progress pushed to the browser over Server-Sent Events, so the UI never polls.
Workers publish to a Redis channel per job; the API process relays it to the client."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator
from uuid import UUID

TERMINAL = {"complete", "empty", "failed", "cancelled"}
HEARTBEAT_SECONDS = 15


def channel(tenant_id: UUID, job_id: UUID) -> str:
    return f"t:{tenant_id}:job:{job_id}"


async def publish_job_event(redis, tenant_id: UUID, job_id: UUID, payload: dict) -> None:
    if redis is not None:
        await redis.publish(channel(tenant_id, job_id), json.dumps(payload, default=str))


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


async def stream_job_events(redis, tenant_id: UUID, job_id: UUID, initial: dict) -> AsyncIterator[str]:
    yield _frame(initial)
    if redis is None or initial.get("status") in TERMINAL:
        return
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel(tenant_id, job_id))
    try:
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS)
            if message is None:
                yield ": keep-alive\n\n"
                continue
            payload = json.loads(message["data"])
            yield _frame(payload)
            if payload.get("status") in TERMINAL:
                return
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()
