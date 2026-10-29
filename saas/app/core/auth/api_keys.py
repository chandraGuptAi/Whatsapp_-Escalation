"""API keys look like sk_<12 hex>.<secret>. The prefix finds the row; only an HMAC of the
secret is stored, so a database leak does not reveal usable keys."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from ..db import Database, system_transaction
from ..errors import Unauthorized
from ..tenancy import Principal

PREFIX = "sk_"


def hash_secret(pepper: str, secret: str) -> str:
    return hmac.new(pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()


def generate(pepper: str) -> tuple[str, str, str]:
    """Returns (full key shown to the user once, prefix, hash to store)."""
    prefix = PREFIX + secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return f"{prefix}.{secret}", prefix, hash_secret(pepper, secret)


async def authenticate(db: Database, pepper: str, raw_key: str) -> Principal:
    prefix, _, secret = raw_key.partition(".")
    if not secret:
        raise Unauthorized("Invalid API key")
    async with system_transaction(db) as conn:
        cur = await conn.execute(
            "SELECT api_key_id, tenant_id, key_hash, scopes FROM app.resolve_api_key(%s)", (prefix,)
        )
        row = await cur.fetchone()
    if row is None or not hmac.compare_digest(row["key_hash"], hash_secret(pepper, secret)):
        raise Unauthorized("Invalid API key")
    return Principal(tenant_id=row["tenant_id"], role="api", scopes=frozenset(row["scopes"]),
                     api_key_id=row["api_key_id"])
