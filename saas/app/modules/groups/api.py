from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_scope, tenant_conn
from app.core.errors import NotFound
from app.core.tenancy import Principal

from . import repository, service
from .schemas import ClusterIn, ClusterOut, GroupOut, GroupUpdate, MessagePage, SessionOut

router = APIRouter(tags=["groups"])
read, write = require_scope("groups:read"), require_scope("groups:write")


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(_=Depends(read), conn=Depends(tenant_conn)):
    return await repository.list_sessions(conn)


@router.get("", response_model=list[GroupOut])
async def list_groups(active: bool | None = None, _=Depends(read), conn=Depends(tenant_conn)):
    return await repository.list_groups(conn, active=active)


@router.patch("/{group_id}", response_model=GroupOut)
async def update_group(group_id: UUID, body: GroupUpdate, _=Depends(write), conn=Depends(tenant_conn)):
    return await service.update_group(conn, group_id, body.model_dump(exclude_unset=True))


@router.get("/{group_id}/messages", response_model=MessagePage)
async def list_messages(group_id: UUID, before: datetime | None = None,
                        limit: int = Query(default=50, ge=1, le=200), _=Depends(read), conn=Depends(tenant_conn)):
    if await repository.get_group(conn, group_id) is None:
        raise NotFound("Group not found")
    items = await repository.list_messages(conn, group_id, before=before, limit=limit)
    return {"items": items, "next_before": items[-1]["sent_at"] if len(items) == limit else None}


@router.get("/clusters", response_model=list[ClusterOut])
async def list_clusters(_=Depends(read), conn=Depends(tenant_conn)):
    return await repository.list_clusters(conn)


@router.post("/clusters", response_model=ClusterOut, status_code=201)
async def create_cluster(body: ClusterIn, principal: Principal = Depends(write), conn=Depends(tenant_conn)):
    return await service.create_cluster(conn, principal.tenant_id, body.name)
