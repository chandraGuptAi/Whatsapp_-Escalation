"""Reversible masking. Real phone numbers, emails and names are swapped for stable tokens
like <PHONE_a1b2c3> before anything leaves for an AI provider. The real value is stored
encrypted in app.pii_vault and put back only in the report the tenant sees."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from uuid import UUID

from ..security import Cipher

PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "phone": re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"),
}
TOKEN = re.compile(r"<(?:PHONE|EMAIL|NAME|ADDRESS|OTHER)_[0-9a-f]{6}>")


def _normalize(kind: str, value: str) -> str:
    return re.sub(r"\D", "", value)[-10:] if kind == "phone" else value.strip().lower()


class Redactor:
    """One instance per AI request. Must use a connection inside the tenant's transaction."""

    def __init__(self, conn, cipher: Cipher, tenant_id: UUID, pepper: str):
        self.conn, self.cipher, self.tenant_id, self.pepper = conn, cipher, tenant_id, pepper
        self.count = 0
        self._tokens: dict[tuple[str, str], str] = {}

    async def redact_text(self, text: str, keep: frozenset[str] = frozenset()) -> str:
        """keep: values the rules engine already identified as non-personal (AWB numbers look
        exactly like 10-digit mobile numbers and must reach the model unmasked)."""
        for kind, pattern in PATTERNS.items():
            for value in set(pattern.findall(text)):
                if re.sub(r"\D", "", value) in keep:
                    continue
                text = text.replace(value, await self.token_for(kind, value))
        return text

    async def token_for(self, kind: str, value: str) -> str:
        normalized = _normalize(kind, value)
        cached = self._tokens.get((kind, normalized))
        if cached:
            self.count += 1
            return cached
        value_hash = hmac.new(self.pepper.encode(), f"{kind}:{normalized}".encode(), hashlib.sha256).hexdigest()
        cur = await self.conn.execute(
            """INSERT INTO app.pii_vault (tenant_id, token, kind, value_hash, value_enc)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, kind, value_hash) DO NOTHING RETURNING token""",
            (self.tenant_id, f"<{kind.upper()}_{secrets.token_hex(3)}>", kind, value_hash,
             self.cipher.encrypt(value.strip(), self.tenant_id)),
        )
        row = await cur.fetchone()
        if row is None:
            cur = await self.conn.execute(
                "SELECT token FROM app.pii_vault WHERE kind = %s AND value_hash = %s", (kind, value_hash)
            )
            row = await cur.fetchone()
        self._tokens[(kind, normalized)] = row["token"]
        self.count += 1
        return row["token"]


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _replace(value, mapping: dict[str, str]):
    if isinstance(value, str):
        return TOKEN.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)
    if isinstance(value, dict):
        return {k: _replace(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace(v, mapping) for v in value]
    return value


async def restore(conn, cipher: Cipher, tenant_id: UUID, value):
    """Puts real values back into every string inside a JSON-like value (for the tenant's own report)."""
    tokens = sorted({t for s in _strings(value) for t in TOKEN.findall(s)})
    if not tokens:
        return value
    cur = await conn.execute("SELECT token, value_enc FROM app.pii_vault WHERE token = ANY(%s)", (tokens,))
    mapping = {row["token"]: cipher.decrypt(row["value_enc"], tenant_id) for row in await cur.fetchall()}
    return _replace(value, mapping)
