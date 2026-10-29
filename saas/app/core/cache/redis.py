"""Redis cache. Every key starts with the tenant id, so cached data never crosses tenants."""
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable
from uuid import UUID

logger = logging.getLogger("app.cache")


async def connect(url: str, *, required: bool):
    """Returns a client, or None when Redis is optional (dev) and unreachable."""
    if not url:
        if required:
            raise RuntimeError("REDIS_URL must be configured")
        return None
    import redis.asyncio as aioredis

    client = aioredis.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        if required:
            raise
        logger.warning("Redis unreachable at %s; caching and rate limits disabled", url)
        return None
    return client


def tenant_key(tenant_id: UUID, *parts: object) -> str:
    return ":".join(["t", str(tenant_id), *map(str, parts)])


async def cached_json(redis, key: str, ttl_seconds: int, loader: Callable[[], Awaitable[Any]]) -> Any:
    if redis is None:
        return await loader()
    hit = await redis.get(key)
    if hit is not None:
        return json.loads(hit)
    value = await loader()
    await redis.set(key, json.dumps(value, default=str), ex=ttl_seconds)
    return value


async def invalidate(redis, key: str) -> None:
    if redis is not None:
        await redis.delete(key)
