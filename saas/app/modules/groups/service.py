"""Business rules for groups and clusters. No SQL here, no HTTP here."""
from __future__ import annotations

from uuid import UUID

from app.core.errors import Conflict, NotFound

from . import repository


async def update_group(conn, group_id: UUID, changes: dict) -> dict:
    if await repository.get_group(conn, group_id) is None:
        raise NotFound("Group not found")
    if changes.get("cluster_id") and not await repository.cluster_exists(conn, changes["cluster_id"]):
        raise NotFound("Cluster not found")
    if "name_override" in changes and changes["name_override"] is not None:
        changes["name_override"] = changes["name_override"].strip() or None
    await repository.update_group(conn, group_id, changes)
    return await repository.get_group(conn, group_id)


async def create_cluster(conn, tenant_id: UUID, name: str) -> dict:
    cluster = await repository.create_cluster(conn, tenant_id, name.strip())
    if cluster is None:
        raise Conflict("A cluster with this name already exists")
    return cluster
