"""Saved report settings (report_presets) and their schedules, for any report module."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..auth import require_scope, tenant_conn
from ..errors import AppError, NotFound
from ..tenancy import Principal

router = APIRouter(prefix="/v1/presets", tags=["presets"])
write = require_scope("reports:write")


class PresetIn(BaseModel):
    module_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    name: str = Field(min_length=1, max_length=120)
    options: dict = Field(default_factory=dict)


class ScheduleIn(BaseModel):
    cron: str = Field(min_length=9, max_length=100, examples=["0 9 * * 1-5"])
    timezone: str = "Asia/Kolkata"
    enabled: bool = True


def next_run(cron: str, timezone: str, after: datetime | None = None) -> datetime:
    from croniter import croniter

    try:
        zone = ZoneInfo(timezone)
        return croniter(cron, (after or datetime.now(zone)).astimezone(zone)).get_next(datetime)
    except (ValueError, KeyError) as exc:
        raise AppError(f"Invalid schedule: {exc}", code="invalid_schedule") from None


@router.get("")
async def list_presets(module: str | None = None, _=Depends(require_scope("reports:read")), conn=Depends(tenant_conn)):
    cur = await conn.execute(
        """SELECT p.id, p.module_key, p.name, p.options, p.updated_at,
                  coalesce(json_agg(json_build_object('id', s.id, 'cron', s.cron, 'timezone', s.timezone,
                           'enabled', s.enabled, 'next_run_at', s.next_run_at)) FILTER (WHERE s.id IS NOT NULL),
                           '[]') AS schedules
             FROM app.report_presets p LEFT JOIN app.schedules s ON s.preset_id = p.id
            WHERE (%(module)s::text IS NULL OR p.module_key = %(module)s)
            GROUP BY p.id ORDER BY p.name""",
        {"module": module},
    )
    return await cur.fetchall()


@router.post("", status_code=201)
async def create_preset(body: PresetIn, request: Request, principal: Principal = Depends(write),
                        conn=Depends(tenant_conn)):
    from psycopg.types.json import Jsonb

    if body.module_key not in {m.key for m in request.app.state.modules}:
        raise NotFound("Unknown module")
    cur = await conn.execute(
        """INSERT INTO app.report_presets (tenant_id, module_key, owner_user_id, name, options)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (principal.tenant_id, body.module_key, principal.user_id, body.name, Jsonb(body.options)),
    )
    return await cur.fetchone()


@router.delete("/{preset_id}", status_code=204)
async def delete_preset(preset_id: UUID, _=Depends(write), conn=Depends(tenant_conn)):
    cur = await conn.execute("DELETE FROM app.report_presets WHERE id = %s", (preset_id,))
    if cur.rowcount == 0:
        raise NotFound("Preset not found")


@router.post("/{preset_id}/schedules", status_code=201)
async def add_schedule(preset_id: UUID, body: ScheduleIn, principal: Principal = Depends(write),
                       conn=Depends(tenant_conn)):
    cur = await conn.execute("SELECT 1 FROM app.report_presets WHERE id = %s", (preset_id,))
    if await cur.fetchone() is None:
        raise NotFound("Preset not found")
    cur = await conn.execute(
        """INSERT INTO app.schedules (tenant_id, preset_id, cron, timezone, enabled, next_run_at)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, next_run_at""",
        (principal.tenant_id, preset_id, body.cron, body.timezone, body.enabled,
         next_run(body.cron, body.timezone) if body.enabled else None),
    )
    return await cur.fetchone()
