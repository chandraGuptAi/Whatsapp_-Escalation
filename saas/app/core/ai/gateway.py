"""The only code allowed to send tenant data to an AI provider.

Order for every request: keep allowed fields -> mask personal data -> size budget ->
cache lookup -> provider call -> audit row -> cache store. A rerun over unchanged data
is answered from app.analysis_cache without calling the provider at all.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from uuid import UUID

from ..db import Database, tenant_transaction
from ..errors import AppError
from ..security import Cipher
from .redact import Redactor

CACHE_TTL_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class AIRequest:
    purpose: str
    prompt_version: str
    system_prompt: str
    records: list[dict]
    allowed_fields: tuple[str, ...]
    # field -> pii kind, for fields that are personal as a whole (e.g. sender_name -> "name")
    personal_fields: dict[str, str] = field(default_factory=dict)
    keep_values: frozenset[str] = frozenset()
    job_id: UUID | None = None


class AIGateway:
    def __init__(self, provider, cipher: Cipher | None, pepper: str, max_input_tokens: int):
        self.provider, self.cipher, self.pepper, self.max_input_tokens = provider, cipher, pepper, max_input_tokens

    async def complete_json(self, db: Database, tenant_id: UUID, request: AIRequest) -> dict:
        """Uses its own short transactions: no connection is held while the provider runs,
        and audit rows are committed even when the call fails."""
        if self.cipher is None:
            raise RuntimeError("DATA_ENCRYPTION_KEY must be configured before AI calls")
        async with tenant_transaction(db, tenant_id) as conn:
            redactor = Redactor(conn, self.cipher, tenant_id, self.pepper)
            payload = [await self._prepare(redactor, record, request) for record in request.records]
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(f"{request.prompt_version}\n{request.system_prompt}\n{body}".encode()).hexdigest()
            audit = {"tenant_id": tenant_id, "job_id": request.job_id, "purpose": request.purpose,
                     "prompt_version": request.prompt_version, "payload_hash": digest,
                     "fields_sent": list(request.allowed_fields), "redaction_count": redactor.count}
            if len(body) // 3 > self.max_input_tokens:
                await self._audit(conn, audit, status="rejected", error="input over token budget")
                hit, rejected = None, True
            else:
                cur = await conn.execute(
                    """UPDATE app.analysis_cache SET hit_count = hit_count + 1
                        WHERE content_hash = %s AND prompt_version = %s AND expires_at > now() RETURNING result""",
                    (digest, request.prompt_version),
                )
                hit, rejected = await cur.fetchone(), False
                if hit is not None:
                    await self._audit(conn, audit, status="ok", cache_hit=True)
        if rejected:
            raise AppError("Too much data for one report; narrow the groups or time window", code="too_large")
        if hit is not None:
            return hit["result"]

        started = time.monotonic()
        try:
            result, usage = await self.provider.complete_json(request.system_prompt, body)
        except Exception as exc:
            async with tenant_transaction(db, tenant_id) as conn:
                await self._audit(conn, audit, status="error", error=f"{type(exc).__name__}: {exc}"[:500],
                                  latency_ms=int((time.monotonic() - started) * 1000))
            raise
        latency_ms = int((time.monotonic() - started) * 1000)

        from psycopg.types.json import Jsonb

        async with tenant_transaction(db, tenant_id) as conn:
            await self._audit(conn, audit, status="ok", latency_ms=latency_ms, **usage)
            await conn.execute(
                """INSERT INTO app.analysis_cache (tenant_id, content_hash, prompt_version, result, expires_at)
                   VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))
                   ON CONFLICT (tenant_id, content_hash, prompt_version)
                   DO UPDATE SET result = EXCLUDED.result, expires_at = EXCLUDED.expires_at""",
                (tenant_id, digest, request.prompt_version, Jsonb(result), CACHE_TTL_SECONDS),
            )
        return result

    async def _prepare(self, redactor: Redactor, record: dict, request: AIRequest) -> dict:
        row = {}
        for name in request.allowed_fields:
            value = record.get(name)
            if value is None or value == "":
                continue
            if name in request.personal_fields:
                value = await redactor.token_for(request.personal_fields[name], str(value))
            elif isinstance(value, str):
                value = await redactor.redact_text(value, request.keep_values)
            row[name] = value
        return row

    async def _audit(self, conn, audit: dict, *, status: str, error: str | None = None, cache_hit: bool = False,
                     latency_ms: int | None = None, tokens_in: int = 0, tokens_out: int = 0) -> None:
        await conn.execute(
            """INSERT INTO app.ai_calls (tenant_id, job_id, provider, model, purpose, prompt_version, payload_hash,
                                         fields_sent, redaction_count, tokens_in, tokens_out, latency_ms,
                                         cache_hit, status, error)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (audit["tenant_id"], audit["job_id"], self.provider.name, self.provider.model, audit["purpose"],
             audit["prompt_version"], audit["payload_hash"], audit["fields_sent"], audit["redaction_count"],
             tokens_in, tokens_out, latency_ms, cache_hit, status, error),
        )
