"""Platform auth endpoints: login, refresh, current user, API key management."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..db import system_transaction, tenant_transaction
from ..errors import NotFound, Unauthorized
from ..tenancy import Principal
from . import api_keys
from .dependencies import current_principal, require_scope, tenant_conn
from .passwords import verify_password
from .tokens import hash_refresh_token, issue_access_token, new_refresh_token

router = APIRouter(prefix="/v1", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    password: str = Field(min_length=1, max_length=200)
    tenant_id: UUID | None = None


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: UUID


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=20)
    expires_in_days: int | None = Field(default=None, ge=1, le=730)


async def _issue(request: Request, *, user_id: UUID, tenant_id: UUID, revoke_session: UUID | None = None) -> TokenOut:
    settings = request.app.state.settings
    refresh, refresh_hash = new_refresh_token()
    expires = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_days)
    async with tenant_transaction(request.app.state.db, tenant_id) as conn:
        cur = await conn.execute("SELECT role FROM app.tenant_users WHERE user_id = %s", (user_id,))
        member = await cur.fetchone()
        if member is None:
            raise Unauthorized("No access to this account")
        if revoke_session is not None:
            await conn.execute("UPDATE app.auth_sessions SET revoked_at = now() WHERE id = %s", (revoke_session,))
        await conn.execute(
            """INSERT INTO app.auth_sessions (tenant_id, user_id, token_hash, user_agent, ip, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (tenant_id, user_id, refresh_hash, request.headers.get("user-agent", "")[:300],
             request.client.host if request.client else None, expires),
        )
    access = issue_access_token(settings.jwt_secret, settings.access_token_minutes,
                                user_id=user_id, tenant_id=tenant_id, role=member["role"])
    return TokenOut(access_token=access, refresh_token=refresh, expires_in=settings.access_token_minutes * 60,
                    tenant_id=tenant_id)


@router.post("/auth/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request):
    async with system_transaction(request.app.state.db) as conn:
        cur = await conn.execute(
            "SELECT id, password_hash FROM app.users WHERE email = %s AND status = 'active'", (body.email,)
        )
        user = await cur.fetchone()
        if user is None or not verify_password(user["password_hash"], body.password):
            raise Unauthorized("Invalid email or password")
        cur = await conn.execute("SELECT tenant_id FROM app.user_memberships(%s)", (user["id"],))
        tenants = [r["tenant_id"] for r in await cur.fetchall()]
        await conn.execute("UPDATE app.users SET last_login_at = now() WHERE id = %s", (user["id"],))
    if not tenants:
        raise Unauthorized("No active account for this user")
    tenant_id = body.tenant_id or tenants[0]
    if tenant_id not in tenants:
        raise Unauthorized("No access to this account")
    return await _issue(request, user_id=user["id"], tenant_id=tenant_id)


@router.post("/auth/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, request: Request):
    async with system_transaction(request.app.state.db) as conn:
        cur = await conn.execute(
            "SELECT session_id, tenant_id, user_id FROM app.resolve_auth_session(%s)",
            (hash_refresh_token(body.refresh_token),),
        )
        session = await cur.fetchone()
    if session is None:
        raise Unauthorized("Session expired; sign in again")
    return await _issue(request, user_id=session["user_id"], tenant_id=session["tenant_id"],
                        revoke_session=session["session_id"])


@router.get("/me")
async def me(principal: Principal = Depends(current_principal), conn=Depends(tenant_conn)):
    cur = await conn.execute("SELECT id, name, plan, timezone FROM app.tenants")
    tenant = await cur.fetchone()
    cur = await conn.execute("SELECT module_key FROM app.tenant_modules WHERE enabled ORDER BY module_key")
    modules = [r["module_key"] for r in await cur.fetchall()]
    return {"tenant": tenant, "role": principal.role, "scopes": sorted(principal.scopes), "modules": modules}


@router.post("/api-keys", status_code=201)
async def create_api_key(body: ApiKeyIn, request: Request,
                         principal: Principal = Depends(require_scope("keys:write")), conn=Depends(tenant_conn)):
    full_key, prefix, key_hash = api_keys.generate(request.app.state.settings.api_key_pepper)
    expires = (datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)) if body.expires_in_days else None
    cur = await conn.execute(
        """INSERT INTO app.api_keys (tenant_id, name, prefix, key_hash, scopes, created_by, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (principal.tenant_id, body.name, prefix, key_hash, sorted(set(body.scopes)), principal.user_id, expires),
    )
    row = await cur.fetchone()
    return {"id": row["id"], "key": full_key, "note": "Store this key now; it is not shown again."}


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: UUID, _: Principal = Depends(require_scope("keys:write")), conn=Depends(tenant_conn)):
    cur = await conn.execute(
        "UPDATE app.api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (key_id,)
    )
    if cur.rowcount == 0:
        raise NotFound("API key not found")
