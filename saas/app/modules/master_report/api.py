from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.auth import require_scope, tenant_conn
from app.core.reports import RunAccepted, start_group_run
from app.core.tenancy import Principal

from . import service
from .schemas import MasterRunRequest

router = APIRouter(tags=["master_report"])


@router.post("/runs", response_model=RunAccepted, status_code=202)
async def start_run(body: MasterRunRequest, principal: Principal = Depends(require_scope("reports:write")),
                    conn=Depends(tenant_conn)):
    return await start_group_run(conn, principal, module_key=service.KEY, kind="generate", body=body,
                                 extra={"title": body.title})
