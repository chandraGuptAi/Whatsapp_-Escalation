"""Shared report generation: load -> rules -> AI gateway -> validate (one corrective
retry) -> save. Report modules supply only their prompt and options."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime
from uuid import UUID

from ..ai import AIRequest, validate_findings
from ..ai.redact import restore
from ..queue import JobContext, JobEmpty
from .rules import extract_facts

MAX_MESSAGES = 5000
REPORT_FIELDS = ("id", "group", "sender_name", "sent_at", "body", "media_text")


class EmptyReport(JobEmpty):
    """No messages in the requested window."""


async def generate_group_report(ctx: JobContext, *, purpose: str, prompt_version: str, system_prompt: str,
                                title: str, question: str | None = None) -> UUID:
    gateway, cipher = ctx.services["ai"], ctx.services["cipher"]
    group_ids = [UUID(g) for g in ctx.options["group_ids"]]
    start = datetime.fromisoformat(ctx.options["window_start"])
    end = datetime.fromisoformat(ctx.options["window_end"])

    await ctx.progress(5, "Loading messages")
    async with ctx.tenant() as conn:
        cur = await conn.execute(
            """SELECT m.id, g.name AS "group", m.sender_name, m.sent_at, m.body, m.media_text
                 FROM app.messages m JOIN app.wa_groups g ON g.id = m.group_id
                WHERE m.group_id = ANY(%s) AND m.sent_at >= %s AND m.sent_at < %s
                ORDER BY m.sent_at LIMIT %s""",
            (group_ids, start, end, MAX_MESSAGES),
        )
        rows = await cur.fetchall()
    if not rows:
        raise EmptyReport("No messages in this window")

    facts = extract_facts(rows)
    records = [{**r, "id": str(r["id"]), "sent_at": r["sent_at"].isoformat()} for r in rows]
    if question:
        records.insert(0, {"id": "question", "body": question})
    source = {str(r["id"]): f"{r['body'] or ''} {r['media_text'] or ''}" for r in rows}

    await ctx.progress(30, "Analysing")
    request = AIRequest(purpose=purpose, prompt_version=prompt_version, system_prompt=system_prompt,
                        records=records, allowed_fields=REPORT_FIELDS, personal_fields={"sender_name": "name"},
                        keep_values=facts.awbs, job_id=ctx.job_id)
    result = await gateway.complete_json(ctx.db, ctx.tenant_id, request)
    issues = validate_findings(result, source)
    if issues:
        await ctx.progress(70, "Re-checking findings")
        retry = replace(request, prompt_version=f"{prompt_version}+fix",
                        system_prompt=f"{system_prompt}\n\nYour previous answer failed these checks; "
                                      f"fix them and cite only listed message ids:\n{json.dumps(issues)}")
        result = await gateway.complete_json(ctx.db, ctx.tenant_id, retry)
        issues = validate_findings(result, source)

    await ctx.progress(90, "Saving")
    from psycopg.types.json import Jsonb

    async with ctx.tenant() as conn:
        content = await restore(conn, cipher, ctx.tenant_id, dict(result))
        content["facts"] = facts.as_dict()
        cur = await conn.execute(
            """INSERT INTO app.reports (tenant_id, job_id, module_key, title, window_start, window_end, content,
                                        summary_md, content_hash, prompt_version, model, validation_status,
                                        validation_notes, created_by)
               SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, j.requested_by
                 FROM app.jobs j WHERE j.id = %s
               RETURNING id""",
            (ctx.tenant_id, ctx.job_id, ctx.module_key, title, start, end, Jsonb(content),
             str(content.get("summary", "")), hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
             prompt_version, gateway.provider.model, "needs_review" if issues else "passed", Jsonb(issues),
             ctx.job_id),
        )
        report_id = (await cur.fetchone())["id"]
        await conn.executemany(
            "INSERT INTO app.report_groups (tenant_id, report_id, group_id) VALUES (%s, %s, %s)",
            [(ctx.tenant_id, report_id, g) for g in group_ids],
        )
    return report_id
