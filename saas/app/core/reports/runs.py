"""Starting a report run, shared by every report module: validate input, enqueue once."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from ..errors import NotFound
from ..queue import enqueue
from ..tenancy import Principal

MAX_WINDOW = timedelta(days=31)


class GroupRunRequest(BaseModel):
    group_ids: list[UUID] = Field(min_length=1, max_length=200)
    window_start: datetime
    window_end: datetime
    # Client-generated per click (e.g. a UUID). Retrying with the same key returns the same job.
    idempotency_key: str = Field(min_length=8, max_length=200)

    @model_validator(mode="after")
    def check_window(self):
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            raise ValueError("window_start and window_end must include a timezone")
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if self.window_end - self.window_start > MAX_WINDOW:
            raise ValueError("window must be 31 days or less")
        if self.window_start > datetime.now(timezone.utc):
            raise ValueError("window_start is in the future")
        return self


class RunAccepted(BaseModel):
    job_id: UUID
    status: str
    created: bool


async def require_groups(conn, group_ids: list[UUID]) -> None:
    cur = await conn.execute("SELECT count(*) AS n FROM app.wa_groups WHERE id = ANY(%s)", (group_ids,))
    if (await cur.fetchone())["n"] != len(set(group_ids)):
        raise NotFound("One or more groups were not found")


async def start_group_run(conn, principal: Principal, *, module_key: str, kind: str, body: GroupRunRequest,
                          extra: dict | None = None) -> RunAccepted:
    await require_groups(conn, body.group_ids)
    options = {"group_ids": [str(g) for g in sorted(set(body.group_ids))],
               "window_start": body.window_start.isoformat(), "window_end": body.window_end.isoformat(),
               **(extra or {})}
    job, created = await enqueue(conn, tenant_id=principal.tenant_id, module_key=module_key, kind=kind,
                                 idempotency_key=f"{module_key}:{body.idempotency_key}", options=options,
                                 requested_by=principal.user_id)
    return RunAccepted(job_id=job["id"], status=job["status"], created=created)
