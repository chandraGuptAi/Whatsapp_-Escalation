"""Who is calling, for which tenant, and what they may do."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

# Role -> scopes. API keys carry explicit scopes instead of a role.
ROLE_SCOPES: dict[str, frozenset[str]] = {
    "owner": frozenset({"*"}),
    "admin": frozenset({"*"}),
    "analyst": frozenset({"groups:read", "groups:write", "reports:read", "reports:write"}),
    "viewer": frozenset({"groups:read", "reports:read"}),
}


@dataclass(frozen=True)
class Principal:
    tenant_id: UUID
    role: str
    scopes: frozenset[str]
    user_id: UUID | None = None
    api_key_id: UUID | None = None

    def can(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


current_principal_var: ContextVar[Principal | None] = ContextVar("current_principal", default=None)
