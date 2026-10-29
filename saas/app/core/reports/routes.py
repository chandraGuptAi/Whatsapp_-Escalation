"""Reading reports, shared by every report module."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from ..auth import require_scope, tenant_conn
from ..errors import NotFound

router = APIRouter(prefix="/v1/reports", tags=["reports"])

_LIST_COLUMNS = ("id, module_key, title, window_start, window_end, validation_status, created_at")


@router.get("")
async def list_reports(module: str | None = None, before: datetime | None = None,
                       limit: int = Query(default=25, ge=1, le=100),
                       _=Depends(require_scope("reports:read")), conn=Depends(tenant_conn)):
    cur = await conn.execute(
        f"""SELECT {_LIST_COLUMNS} FROM app.reports
             WHERE (%(module)s::text IS NULL OR module_key = %(module)s)
               AND (%(before)s::timestamptz IS NULL OR created_at < %(before)s)
             ORDER BY created_at DESC LIMIT %(limit)s""",
        {"module": module, "before": before, "limit": limit},
    )
    items = await cur.fetchall()
    return {"items": items, "next_before": items[-1]["created_at"] if len(items) == limit else None}


@router.get("/{report_id}")
async def get_report(report_id: UUID, _=Depends(require_scope("reports:read")), conn=Depends(tenant_conn)):
    cur = await conn.execute(
        f"""SELECT {_LIST_COLUMNS}, content, summary_md, prompt_version, model, validation_notes,
                   (SELECT coalesce(array_agg(group_id), '{{}}') FROM app.report_groups WHERE report_id = r.id) AS group_ids
              FROM app.reports r WHERE id = %s""",
        (report_id,),
    )
    report = await cur.fetchone()
    if report is None:
        raise NotFound("Report not found")
    return report
