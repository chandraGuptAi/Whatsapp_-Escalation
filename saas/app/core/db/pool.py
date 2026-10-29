"""One async connection pool per process; connections return rows as dicts."""
from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool


def use_selector_loop_on_windows() -> None:
    """psycopg's async driver cannot use Windows' default Proactor loop. Call before asyncio starts."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Database:
    def __init__(self, url: str, *, min_size: int = 2, max_size: int = 20):
        self._url, self._min, self._max = url, min_size, max_size
        self._pool: AsyncConnectionPool | None = None

    async def open(self) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(
            self._url,
            min_size=self._min,
            max_size=self._max,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await self._pool.open(wait=True, timeout=10)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("Database pool is not open")
        return self._pool
