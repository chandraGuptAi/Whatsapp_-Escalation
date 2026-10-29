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
from dataclasses import asdict, dataclass

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


async def verify_erp_token(client: httpx.AsyncClient, redis, provider: dict, erp_token: str) -> ErpIdentity:
    token_key = "erp:" + hashlib.sha256(f"{provider['provider_id']}:{erp_token}".encode()).hexdigest()
    if redis is not None and (cached := await redis.get(token_key)):
        return ErpIdentity(**json.loads(cached))

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

    identity = identity_from_reply(reply, config)
    if redis is not None:
        await redis.set(token_key, json.dumps(asdict(identity)), ex=VERIFIED_CACHE_SECONDS)
    return identity
