"""Job status shared by every module: read, live progress, cancel."""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..auth import require_scope, tenant_conn
from ..db import tenant_transaction
from ..errors import Conflict, NotFound
from ..events import publish_job_event, stream_job_events
from ..tenancy import Principal

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

_COLUMNS = "id, module_key, kind, status, progress, error, created_at, started_at, finished_at"


async def _get(conn, job_id: UUID) -> dict:
    cur = await conn.execute(f"SELECT {_COLUMNS} FROM app.jobs WHERE id = %s", (job_id,))
    job = await cur.fetchone()
    if job is None:
        raise NotFound("Job not found")
    return job


@router.get("/{job_id}")
async def get_job(job_id: UUID, _: Principal = Depends(require_scope("reports:read")), conn=Depends(tenant_conn)):
    return await _get(conn, job_id)


@router.get("/{job_id}/events")
async def job_events(job_id: UUID, request: Request, principal: Principal = Depends(require_scope("reports:read"))):
    # Short transaction for the first read only; the stream itself must not hold a pooled connection.
    async with tenant_transaction(request.app.state.db, principal.tenant_id) as conn:
        job = await _get(conn, job_id)
    stream = stream_job_events(request.app.state.redis, principal.tenant_id, job_id, job)
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: UUID, request: Request,
                     principal: Principal = Depends(require_scope("reports:write")), conn=Depends(tenant_conn)):
    cur = await conn.execute(
        """UPDATE app.jobs SET status = 'cancelled', finished_at = now(), locked_by = NULL, locked_until = NULL
            WHERE id = %s AND status IN ('queued', 'running') RETURNING id""",
        (job_id,),
    )
    if await cur.fetchone() is None:
        await _get(conn, job_id)
        raise Conflict("Job already finished")
    await publish_job_event(request.app.state.redis, principal.tenant_id, job_id, {"status": "cancelled"})
    return {"id": job_id, "status": "cancelled"}
