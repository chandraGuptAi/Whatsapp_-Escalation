"""The only way to get a database connection. Each transaction is pinned to one tenant.

app.tenant_id is set with is_local=true, so it is cleared at commit/rollback and a pooled
connection can never carry one tenant's context into another tenant's request.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator
from uuid import UUID

if TYPE_CHECKING:
    from psycopg import AsyncConnection

    from .pool import Database


@asynccontextmanager
async def tenant_transaction(db: "Database", tenant_id: UUID) -> AsyncIterator["AsyncConnection"]:
    async with db.pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        yield conn


@asynccontextmanager
async def system_transaction(db: "Database") -> AsyncIterator["AsyncConnection"]:
    """No tenant set: RLS hides every tenant row. Use only for the SECURITY DEFINER lookups
    (resolve_api_key, resolve_auth_session, user_memberships, claim_job, ...)."""
    async with db.pool.connection() as conn, conn.transaction():
        yield conn
