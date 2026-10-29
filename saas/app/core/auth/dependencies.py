"""FastAPI dependencies every route uses: who is calling, may they do this, and a
connection already scoped to their tenant."""
from __future__ import annotations

from typing import AsyncIterator

from fastapi import Depends, Request

from ..db import tenant_transaction
from ..errors import Forbidden, Unauthorized
from ..security import rate_limit
from ..tenancy import Principal, current_principal_var
from . import api_keys, erp_jwt
from .tokens import ISSUER, decode_access_token


async def current_principal(request: Request) -> Principal:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("Bearer token required")
    state = request.app.state
    if token.startswith(api_keys.PREFIX):
        principal = await api_keys.authenticate(state.db, state.settings.api_key_pepper, token)
    elif (issuer := erp_jwt.unverified_issuer(token)) not in (None, ISSUER):
        principal = await erp_jwt.authenticate(state, token, issuer)
    else:
        principal = decode_access_token(state.settings.jwt_secret, token)
    await rate_limit.check(state.redis, principal.tenant_id, state.settings.rate_limit_per_minute)
    current_principal_var.set(principal)
    return principal


def require_scope(scope: str):
    async def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(scope):
            raise Forbidden(f"Missing permission: {scope}")
        return principal

    return dependency


async def tenant_conn(request: Request, principal: Principal = Depends(current_principal)) -> AsyncIterator:
    async with tenant_transaction(request.app.state.db, principal.tenant_id) as conn:
        yield conn
