"""Report pipeline on real PostgreSQL with a fake DeepSeek: masking, validation, saving, cache.
Skipped unless both URLs are set (see test_rls.py)."""
from __future__ import annotations

import asyncio
import base64
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

OWNER_URL, APP_URL = os.getenv("SAAS_TEST_OWNER_URL"), os.getenv("SAAS_TEST_APP_URL")
pytestmark = pytest.mark.skipif(not (OWNER_URL and APP_URL), reason="needs SAAS_TEST_OWNER_URL and SAAS_TEST_APP_URL")


class FakeDeepSeek:
    name, model = "deepseek", "fake-model"

    def __init__(self):
        self.calls = []

    async def complete_json(self, system_prompt, user_content):
        import json

        self.calls.append(user_content)
        rows = [r for r in json.loads(user_content) if r["id"] != "question"]
        delayed = next(r for r in rows if "delayed" in r["body"])
        return {"summary": "One delayed shipment.",
                "findings": [{"text": "AWB 9123456789 is delayed", "severity": "high",
                              "message_ids": [delayed["id"]]}]}, {"tokens_in": 100, "tokens_out": 20}


@pytest.fixture
def seeded():
    import psycopg

    tenant, session, group, job = (uuid.uuid4() for _ in range(4))
    now = datetime.now(timezone.utc)
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("INSERT INTO app.tenants (id, slug, name) VALUES (%s, %s, 'Report tenant')",
                     (tenant, f"rep-{tenant.hex[:8]}"))
        conn.execute("INSERT INTO app.wa_sessions (id, tenant_id) VALUES (%s, %s)", (session, tenant))
        conn.execute("INSERT INTO app.wa_groups (id, tenant_id, session_id, jid, name) VALUES (%s, %s, %s, 'g@g.us', 'G')",
                     (group, tenant, session))
        for i, body in enumerate(["AWB 9123456789 delayed at hub, call 9876543210",
                                  "Please share update", "ok"]):
            conn.execute("""INSERT INTO app.messages (tenant_id, group_id, wa_msg_id, sender_hash, sender_name,
                            body, sent_at) VALUES (%s, %s, %s, 's', 'Ramesh', %s, %s)""",
                         (tenant, group, f"m{i}", body, now - timedelta(minutes=10 - i)))
        conn.execute("""INSERT INTO app.jobs (id, tenant_id, module_key, kind, idempotency_key, status)
                        VALUES (%s, %s, 'master_report', 'generate', %s, 'running')""", (job, tenant, f"rep-{job.hex}"))
    yield tenant, group, job, now
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM app.tenants WHERE id = %s", (tenant,))


def test_report_is_masked_validated_saved_and_cached(seeded):
    import psycopg

    from app.core.ai import AIGateway
    from app.core.db import Database, use_selector_loop_on_windows
    from app.core.queue import JobContext
    from app.core.reports import generate_group_report
    from app.core.security import Cipher

    tenant, group, job, now = seeded
    fake = FakeDeepSeek()
    cipher = Cipher(base64.b64encode(os.urandom(32)).decode())
    options = {"group_ids": [str(group)], "window_start": (now - timedelta(hours=1)).isoformat(),
               "window_end": (now + timedelta(minutes=1)).isoformat()}
    use_selector_loop_on_windows()

    async def scenario():
        db = Database(APP_URL, min_size=1, max_size=4)
        await db.open()
        try:
            ctx = JobContext(db=db, redis=None, services={"ai": AIGateway(fake, cipher, "pepper", 60000),
                                                          "cipher": cipher},
                             job_id=job, tenant_id=tenant, module_key="master_report", kind="generate",
                             options=options, attempts=1)
            report_id = await generate_group_report(ctx, purpose="master_report", prompt_version="test-v1",
                                                    system_prompt="Analyse.", title="Test report")
            return report_id
        finally:
            await db.close()

    report_id = asyncio.run(scenario())

    sent = fake.calls[0]
    assert "9876543210" not in sent and "<PHONE_" in sent   # phone masked before DeepSeek
    assert "9123456789" in sent                              # AWB kept (rules engine found it)
    assert "Ramesh" not in sent and "<NAME_" in sent         # sender names masked
    with psycopg.connect(OWNER_URL) as conn:
        status, content, groups = conn.execute(
            """SELECT validation_status, content,
                      (SELECT array_agg(group_id) FROM app.report_groups WHERE report_id = r.id)
                 FROM app.reports r WHERE id = %s""", (report_id,)).fetchone()
        calls = conn.execute("SELECT status, cache_hit, redaction_count > 0 FROM app.ai_calls WHERE tenant_id = %s",
                             (tenant,)).fetchall()
    assert status == "passed"
    assert content["facts"]["message_count"] == 3 and content["facts"]["unique_awbs"] == 1
    assert content["facts"]["status_counts"] == {"delay": 1}
    assert groups == [group]
    assert calls == [("ok", False, True)]


def test_text_answers_are_masked_on_the_way_out_and_restored_on_the_way_back(seeded):
    from app.core.ai import AIGateway, AIRequest
    from app.core.db import Database, use_selector_loop_on_windows
    from app.core.security import Cipher

    tenant, *_ = seeded
    seen = []

    class EchoProvider:
        name, model = "deepseek", "fake-model"

        async def complete_text(self, system_prompt, user_content):
            seen.append(user_content)
            return f"| Seller | Note |\n|---|---|\n{user_content}", {"tokens_in": 5, "tokens_out": 5}

    gateway = AIGateway(EchoProvider(), Cipher(base64.b64encode(os.urandom(32)).decode()), "pepper", 60000)
    use_selector_loop_on_windows()

    async def scenario():
        db = Database(APP_URL, min_size=1, max_size=2)
        await db.open()
        try:
            request = AIRequest(purpose="master_report", prompt_version="t-v1", system_prompt="Report.",
                                text="Ramesh FE: AWB 9123456789 stuck, call 9876543210",
                                personal_values={"Ramesh FE": "name"}, keep_values=frozenset({"9123456789"}))
            first = await gateway.complete_text(db, tenant, request)
            second = await gateway.complete_text(db, tenant, request)  # served from cache
            return first, second
        finally:
            await db.close()

    first, second = asyncio.run(scenario())
    assert len(seen) == 1
    assert "Ramesh FE" not in seen[0] and "9876543210" not in seen[0] and "9123456789" in seen[0]
    assert "Ramesh FE" in first and "9876543210" in first and first == second
