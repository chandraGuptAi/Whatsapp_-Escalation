"""ERP in front of the module: every request carries a short-lived JWT signed by the ERP.

Verified locally with the ERP's public key (no call to the ERP, no DB lookup on the hot path):
algorithm pinned per provider, issuer, audience, expiry and a maximum lifetime are all checked.
The user is created/updated on first sight and remembered for a few minutes.
"""
from __future__ import annotations

import hashlib
import json
import time
from uuid import UUID

from ..cache import cached_json
from ..db import system_transaction
from ..errors import Unauthorized
from ..tenancy import ROLE_SCOPES, Principal
from . import erp
from .tokens import AUDIENCE

ASYMMETRIC = {"RS256", "RS384", "RS512", "ES256", "ES384", "PS256"}
PROVIDER_CACHE_SECONDS = 300
USER_CACHE_SECONDS = 300
DEFAULT_MAX_TTL = 300
LEEWAY_SECONDS = 10

_providers: dict[str, tuple[float, dict | None]] = {}


def unverified_issuer(token: str) -> str | None:
    import jwt

    try:
        return jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        return None


async def _provider(db, issuer: str) -> dict | None:
    hit = _providers.get(issuer)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    async with system_transaction(db) as conn:
        cur = await conn.execute("SELECT * FROM app.resolve_identity_provider_by_issuer(%s)", (issuer,))
        provider = await cur.fetchone()
    _providers[issuer] = (time.monotonic() + PROVIDER_CACHE_SECONDS, provider)
    return provider


def verify_claims(token: str, config: dict) -> dict:
    import jwt

    algorithm = config.get("algorithm", "RS256")
    if algorithm not in ASYMMETRIC:
        raise Unauthorized("Sign-in provider is misconfigured")
    try:
        claims = jwt.decode(token, config["public_key"], algorithms=[algorithm], audience=AUDIENCE,
                            issuer=config["issuer"], leeway=LEEWAY_SECONDS,
                            options={"require": ["exp", "iat", "sub", "iss", "aud"]})
    except jwt.PyJWTError:
        raise Unauthorized("Invalid or expired sign-in token") from None
    if claims["exp"] - claims["iat"] > int(config.get("max_ttl_seconds", DEFAULT_MAX_TTL)):
        raise Unauthorized("Sign-in token lifetime is too long")
    if str(claims["sub"]).strip() == "":
        raise Unauthorized("Sign-in token has no user")
    return claims


async def authenticate(state, token: str, issuer: str) -> Principal:
    provider = await _provider(state.db, issuer)
    if provider is None:
        raise Unauthorized("Unknown sign-in issuer")
    config = provider["config"]
    claims = verify_claims(token, config)

    erp_role = claims.get(config.get("role_claim", "role"))
    identity = erp.ErpIdentity(
        external_id=str(claims["sub"]),
        name=str(claims.get(config.get("name_claim", "name")) or "")[:200],
        phone=str(claims.get(config.get("phone_claim", "mobile")) or "") or None,
        role=config.get("role_map", {}).get(str(erp_role)) if erp_role is not None else None,
    )
    fingerprint = hashlib.sha256(json.dumps([str(provider["provider_id"]), identity.external_id, identity.name,
                                             identity.phone, identity.role]).encode()).hexdigest()

    async def load() -> dict:
        phone_hash, phone_enc = erp.phone_fields(state.settings.api_key_pepper, state.services["cipher"],
                                                 provider["tenant_id"], identity.phone)
        row = await erp.upsert_user(state.db, provider["provider_id"], identity, phone_hash, phone_enc)
        return {"user_id": str(row["out_user_id"]), "tenant_id": str(row["out_tenant_id"]), "role": row["out_role"]}

    user = await cached_json(state.redis, f"erpjwt:{fingerprint}", USER_CACHE_SECONDS, load)
    return Principal(tenant_id=UUID(user["tenant_id"]), user_id=UUID(user["user_id"]), role=user["role"],
                     scopes=ROLE_SCOPES[user["role"]])
