"""Sign-in through a customer's ERP.

The ERP token (e.g. a Laravel Sanctum "id|secret" token) cannot be checked locally, so we
call one of the ERP's own "current user" APIs with it, exactly as the ERP frontend does.
If the ERP answers with a user, the token is genuine. Nothing from the browser's
localStorage (role, mobile, user id) is trusted: identity comes only from the ERP's reply.
The ERP token is never stored or logged.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import httpx

from ..errors import AppError, Unauthorized

VERIFY_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
VERIFIED_CACHE_SECONDS = 60


@dataclass(frozen=True)
class ErpIdentity:
    external_id: str
    name: str
    phone: str | None
    role: str | None        # module role from the provider's role_map, None = keep current


def pick(data, path: str | None):
    """Reads a dot path like "data.login_user_detail.MobileNo" or "items.0.id"."""
    if not path:
        return None
    for part in path.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


def identity_from_reply(reply: dict, config: dict) -> ErpIdentity:
    external_id = pick(reply, config.get("user_id_path", "id"))
    if external_id in (None, ""):
        raise Unauthorized("The ERP did not return a signed-in user")
    if config.get("active_path"):
        if str(pick(reply, config["active_path"])) != str(config.get("active_value", "1")):
            raise Unauthorized("This ERP user is not active")
    erp_role = pick(reply, config.get("role_path"))
    role = config.get("role_map", {}).get(str(erp_role)) if erp_role is not None else None
    phone = pick(reply, config.get("phone_path"))
    return ErpIdentity(external_id=str(external_id), name=str(pick(reply, config.get("name_path")) or "")[:200],
                       phone=str(phone) if phone else None, role=role)


async def verify_erp_token(client: httpx.AsyncClient, redis, provider: dict, erp_token: str,
                           claimed_user: dict | None = None) -> ErpIdentity:
    """Normal mode: identity comes from the ERP's reply (needs a "current user" API).

    Simple mode (config "trust_request_identity": true), for ERPs without such an API: the ERP
    call only proves the token belongs to a signed-in ERP user; the user details come from the
    request. The role is never taken from the request: it is managed inside the module.
    Known gap: a signed-in ERP user can claim another user's id. Switch to normal mode when the
    ERP offers a current-user API (configuration change only)."""
    config = provider["config"] or {}
    simple_mode = bool(config.get("trust_request_identity"))
    if simple_mode and not claimed_user:
        raise AppError("User details are required", code="invalid_request", status=422)

    reply = await _erp_reply(client, redis, provider, erp_token)
    if not simple_mode:
        return identity_from_reply(reply, config)
    identity = identity_from_reply(claimed_user, config)
    return ErpIdentity(external_id=identity.external_id, name=identity.name, phone=identity.phone, role=None)


async def _erp_reply(client: httpx.AsyncClient, redis, provider: dict, erp_token: str) -> dict:
    token_key = "erp:" + hashlib.sha256(f"{provider['provider_id']}:{erp_token}".encode()).hexdigest()
    if redis is not None and (cached := await redis.get(token_key)):
        return json.loads(cached)

    config = provider["config"] or {}
    try:
        response = await client.request(
            config.get("method", "GET"), provider["verify_url"], timeout=VERIFY_TIMEOUT,
            headers={"Authorization": f"Bearer {erp_token}", "Accept": "application/json"},
        )
    except httpx.HTTPError:
        raise AppError("Could not reach the ERP to confirm sign-in", code="erp_unavailable", status=502) from None
    if response.status_code in (401, 403, 419):
        raise Unauthorized("ERP session expired; sign in to the ERP again")
    if response.status_code != 200:
        raise AppError("The ERP could not confirm sign-in", code="erp_unavailable", status=502)
    try:
        reply = response.json()
    except ValueError:
        raise AppError("The ERP returned an unexpected reply", code="erp_unavailable", status=502) from None

    if redis is not None:
        await redis.set(token_key, json.dumps(reply), ex=VERIFIED_CACHE_SECONDS)
    return reply
