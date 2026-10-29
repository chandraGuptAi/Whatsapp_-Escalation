"""Per-tenant request limit: one Redis INCR per request, fixed one-minute window."""
from __future__ import annotations

import time
from uuid import UUID

from ..errors import RateLimited


async def check(redis, tenant_id: UUID, limit_per_minute: int) -> None:
    if redis is None or limit_per_minute <= 0:
        return
    key = f"rl:{tenant_id}:{int(time.time() // 60)}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 70)
    if count > limit_per_minute:
        raise RateLimited("Too many requests; retry in a minute")
