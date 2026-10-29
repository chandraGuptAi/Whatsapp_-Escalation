"""Enqueue a job. The same idempotency key always returns the same job, so a double
click, a client retry or a scheduler re-run never starts duplicate work."""
from __future__ import annotations

from uuid import UUID


async def enqueue(conn, *, tenant_id: UUID, module_key: str, kind: str, idempotency_key: str,
                  options: dict, requested_by: UUID | None = None,
                  schedule_id: UUID | None = None) -> tuple[dict, bool]:
    """Returns (job row, created). Must run inside tenant_transaction for tenant_id."""
    from psycopg.types.json import Jsonb

    cur = await conn.execute(
        """INSERT INTO app.jobs (tenant_id, module_key, kind, idempotency_key, options, requested_by, schedule_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
           RETURNING id, status, created_at""",
        (tenant_id, module_key, kind, idempotency_key, Jsonb(options), requested_by, schedule_id),
    )
    row = await cur.fetchone()
    if row is not None:
        return row, True
    cur = await conn.execute(
        "SELECT id, status, created_at FROM app.jobs WHERE idempotency_key = %s", (idempotency_key,)
    )
    return await cur.fetchone(), False
