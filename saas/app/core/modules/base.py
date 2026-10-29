"""The contract every module fulfils. A module's __init__.py exposes MODULE = Module(...)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from fastapi import APIRouter

from ..queue import JobHandler


@dataclass(frozen=True)
class Module:
    key: str                    # matches the folder name and app.modules.key
    name: str
    router: APIRouter | None = None             # mounted at /v1/<key>
    jobs: Mapping[str, JobHandler] = field(default_factory=dict)   # kind -> handler
    core: bool = False          # core modules are always on; others need a tenant_modules row
