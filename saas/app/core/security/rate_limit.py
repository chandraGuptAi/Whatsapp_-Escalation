"""Per-tenant request limit: one Redis INCR per request, fixed one-minute window."""
from __future__ import annotations

import time
from uuid import UUID

from ..errors import RateLimited


async def check(redis, tenant_id: UUID, limit_per_minute: int) -> None:
    await check_key(redis, str(tenant_id), limit_per_minute)


async def check_key(redis, name: str, limit_per_minute: int) -> None:
    """Limit by any key, e.g. "exchange:<ip>" for endpoints used before sign-in."""
    if redis is None or limit_per_minute <= 0:
        return
    key = f"rl:{name}:{int(time.time() // 60)}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 70)
    if count > limit_per_minute:
        raise RateLimited("Too many requests; retry in a minute")
