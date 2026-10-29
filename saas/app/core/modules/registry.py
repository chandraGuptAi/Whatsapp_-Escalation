"""Finds modules by folder, mounts their routes, and hands their job handlers to workers.
Adding a module = adding a folder. Removing one = deleting the folder (or DISABLED_MODULES)."""
from __future__ import annotations

import importlib
import pkgutil
from typing import Iterable

from fastapi import Depends, FastAPI, Request

from ..auth import current_principal
from ..cache import cached_json, tenant_key
from ..db import tenant_transaction
from ..errors import Forbidden
from ..queue import JobHandler
from ..tenancy import Principal
from .base import Module

MODULES_PACKAGE = "app.modules"
ENABLED_CACHE_SECONDS = 60


def discover(disabled: Iterable[str] = ()) -> list[Module]:
    package = importlib.import_module(MODULES_PACKAGE)
    disabled = set(disabled)
    found = []
    for info in pkgutil.iter_modules(package.__path__):
        if not info.ispkg or info.name in disabled:
            continue
        module = getattr(importlib.import_module(f"{MODULES_PACKAGE}.{info.name}"), "MODULE", None)
        if not isinstance(module, Module):
            raise RuntimeError(f"{MODULES_PACKAGE}.{info.name} must define MODULE = Module(...)")
        if module.key != info.name:
            raise RuntimeError(f"Module key {module.key!r} must match its folder {info.name!r}")
        found.append(module)
    return sorted(found, key=lambda m: m.key)


def enabled_modules_key(tenant_id) -> str:
    return tenant_key(tenant_id, "modules")


def require_module(module: Module):
    async def dependency(request: Request, principal: Principal = Depends(current_principal)) -> None:
        if module.core:
            return

        async def load() -> list[str]:
            async with tenant_transaction(request.app.state.db, principal.tenant_id) as conn:
                cur = await conn.execute("SELECT module_key FROM app.tenant_modules WHERE enabled")
                return [r["module_key"] for r in await cur.fetchall()]

        enabled = await cached_json(request.app.state.redis, enabled_modules_key(principal.tenant_id),
                                    ENABLED_CACHE_SECONDS, load)
        if module.key not in enabled:
            raise Forbidden(f"The {module.name} module is not enabled for this account")

    return dependency


def mount(app: FastAPI, modules: list[Module]) -> None:
    for module in modules:
        if module.router is not None:
            app.include_router(module.router, prefix=f"/v1/{module.key}",
                               dependencies=[Depends(require_module(module))])


def job_handlers(modules: list[Module]) -> dict[tuple[str, str], JobHandler]:
    return {(module.key, kind): handler for module in modules for kind, handler in module.jobs.items()}
